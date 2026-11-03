import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import duckdb

from zeezazum.config import ZeezazumConfig
from zeezazum.models import CandidateState, MediaItem, MediaKind, SocialAccount, Post
from zeezazum.pipeline.state import StateStore
from zeezazum.pipeline.orchestrator import (
    MediaQueue,
    RemoteCheckpoints,
    PersistenceFailure,
    run_media_sync,
    _scrape_one,
)
from zeezazum.platforms.base import AuthenticationRequired, ProfileUnavailable
from zeezazum.platforms.instagram.scraper import InstagramScraper
from zeezazum.storage.local import LocalStorage


def config(path):
    return ZeezazumConfig.model_validate(
        {
            "input": {"path": "unused", "url_column": "url", "id_column": "id"},
            "storage": {"local": {"base_path": str(path)}},
            "media": {"max_concurrent_downloads": 2},
        }
    )


class StateTests(unittest.TestCase):
    def test_legacy_migration_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.duckdb"
            con = duckdb.connect(str(path))
            con.execute(
                "CREATE TABLE candidate_state (candidate_id VARCHAR PRIMARY KEY, handle VARCHAR, status VARCHAR, newest_post_id VARCHAR, newest_post_timestamp VARCHAR, total_posts_seen BIGINT, last_run_at VARCHAR, media_pending BIGINT)"
            )
            con.execute(
                "INSERT INTO candidate_state VALUES ('1','Alice','done','abc',NULL,10,NULL,99)"
            )
            con.close()
            state = StateStore(path)
            self.assertEqual(state.get("1", "alice").newest_post_id, "abc")
            self.assertIsNone(state.get("1", "bob"))
            state.upsert(CandidateState("1", "bob", "done", newest_post_id="xyz"))
            copy = Path(tmp) / "copy.duckdb"
            copy.write_bytes(state.snapshot())
            state.close()
            self.assertTrue(Path(str(path) + ".pre-v023.bak").exists())
            state = StateStore(copy)
            self.assertEqual(state.get("1", "bob").newest_post_id, "xyz")
            self.assertEqual(state.get("1", "alice").newest_post_id, "abc")
            state.close()


class RecoveryFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.cfg = config(self.path)
        self.storage = LocalStorage(self.path)
        self.account = SocialAccount(
            "1", "instagram", "alice", "https://instagram.com/alice"
        )
        self.item = MediaItem(MediaKind.IMAGE, "https://cdn/item", 0)
        self.storage.write_json(
            "instagram/alice/posts.json",
            [
                {
                    "shortcode": "abc",
                    "media": [{"kind": "image", "index": 0, "url": self.item.url}],
                }
            ],
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()


class RecoveryTests(RecoveryFixture):
    async def test_failed_download_resume_and_no_redownload(self):
        with patch(
            "zeezazum.platforms.instagram.downloader.MediaDownloader._fetch",
            new=AsyncMock(side_effect=OSError("expired URL")),
        ):
            with self.assertRaises(PersistenceFailure):
                await run_media_sync(self.cfg, [self.account])
        state = StateStore(self.path / "_state.duckdb")
        self.assertEqual(len(state.pending_media("alice")), 1)
        state.close()
        fetch = AsyncMock(return_value=(b"picture", "image/png"))
        with patch(
            "zeezazum.platforms.instagram.downloader.MediaDownloader._fetch", new=fetch
        ):
            await run_media_sync(self.cfg, [self.account, self.account])
            await run_media_sync(self.cfg, [self.account])
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(
            self.storage.read_bytes("instagram/alice/media/abc_0.png"), b"picture"
        )

    async def test_write_failure_and_filters(self):
        state = StateStore(self.path / "state.duckdb")
        state.upsert(CandidateState("1", "alice", "done"))
        queue = MediaQueue(self.cfg, self.storage, state)
        with (
            patch.object(
                queue.downloader,
                "_fetch",
                new=AsyncMock(return_value=(b"x", "image/jpeg")),
            ),
            patch.object(self.storage, "write_bytes", side_effect=OSError("disk full")),
        ):
            await queue.reconcile([self.account])
            await queue.close()
        self.assertEqual(state.get("1", "alice").media_pending, 1)
        self.assertEqual(state.pending_media("alice", images=False), [])
        state.close()

    async def test_remote_failure_retains_recoverable_file(self):
        path = self.path / "state.duckdb"
        state = StateStore(path)
        state.upsert(CandidateState("1", "alice", "done"))
        remote = LocalStorage(self.path / "remote")
        cp = RemoteCheckpoints(self.cfg, remote, state, path)
        await cp.sync()
        recovered = StateStore(self.path / "remote/_state.duckdb")
        self.assertEqual(recovered.get("1", "alice").status, "done")
        recovered.close()
        with patch.object(remote, "write_bytes", side_effect=OSError("offline")):
            with self.assertRaises(PersistenceFailure):
                await cp.close()
        self.assertTrue(path.exists())
        state.close()

    async def test_checkpoint_does_not_advance_cursor_on_auth_failure(self):
        state = StateStore(self.path / "state.duckdb")
        state.upsert(CandidateState("1", "alice", "done", newest_post_id="old"))
        queue = MediaQueue(self.cfg, self.storage, state)
        queue.checkpoints = RemoteCheckpoints(self.cfg, self.storage, state, None)
        scraper = AsyncMock()

        async def extract(*args, **kwargs):
            await kwargs["on_checkpoint"]([Post("new", "url", None, None, ())])
            raise AuthenticationRequired("login")

        scraper.extract_posts.side_effect = extract
        with self.assertRaises(AuthenticationRequired):
            await _scrape_one(
                account=self.account,
                context=None,
                scraper=scraper,
                storage=self.storage,
                state=state,
                queue=queue,
                max_posts=60,
                since_date=None,
                max_attempts=3,
                backoff_seconds=0,
            )
        self.assertEqual(state.get("1", "alice").newest_post_id, "old")
        await queue.close()
        state.close()

    async def test_auth_mid_scroll_closes_page(self):
        page = AsyncMock()
        page.on = lambda *args: None
        page.url = "https://instagram.com/alice"

        async def wheel(*args):
            page.url = "https://instagram.com/challenge/"

        page.mouse.wheel.side_effect = wheel
        context = AsyncMock()
        context.new_page.return_value = page
        with (
            patch(
                "zeezazum.platforms.instagram.scraper.asyncio.sleep", new=AsyncMock()
            ),
            patch.object(
                InstagramScraper,
                "_read_profile_stats",
                new=AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaises(AuthenticationRequired):
                await InstagramScraper().extract_posts(
                    context,
                    "alice",
                    max_posts=60,
                    since_date=None,
                    stop_at_post_id=None,
                )
        page.close.assert_awaited_once()


class IntegrationTests(RecoveryFixture):
    async def test_periodic_snapshot_and_ordered_shutdown(self):
        import threading

        state_path = self.path / "state.duckdb"
        state = StateStore(state_path)
        state.upsert(CandidateState("1", "alice", "done"))
        self.cfg.storage.s3.state_sync_interval_seconds = 0.01
        remote = LocalStorage(self.path / "remote")
        started = threading.Event()
        release = threading.Event()
        original = remote.write_bytes
        calls = []

        def slow_write(key, data):
            calls.append(data)
            if len(calls) == 1:
                started.set()
                release.wait(2)
            original(key, data)

        with patch.object(remote, "write_bytes", side_effect=slow_write):
            cp = RemoteCheckpoints(self.cfg, remote, state, state_path)
            await asyncio.to_thread(started.wait, 2)
            self.assertTrue(started.is_set())
            state.upsert(CandidateState("1", "alice", "done", newest_post_id="latest"))
            closer = asyncio.create_task(cp.close())
            await asyncio.sleep(0)
            self.assertFalse(closer.done())
            release.set()
            await closer
        state.close()
        recovered = StateStore(self.path / "remote/_state.duckdb")
        self.assertEqual(recovered.get("1", "alice").newest_post_id, "latest")
        recovered.close()
        self.assertEqual(len(calls), 2)

    async def test_batch_stops_on_auth_but_continues_unavailable(self):
        from zeezazum.pipeline.orchestrator import run_scrape

        manager = AsyncMock()
        browser = AsyncMock()
        manager.__aenter__.return_value.chromium.launch.return_value = browser
        accounts = [self.account, SocialAccount("1", "instagram", "bob", "url")]
        self.cfg.scraping.delay_between_profiles_seconds = (0, 0)
        for failure, expected_calls in [
            (AuthenticationRequired("renew session"), 1),
            (ProfileUnavailable("private"), 2),
        ]:
            with (
                patch(
                    "zeezazum.pipeline.orchestrator.async_playwright",
                    return_value=manager,
                ),
                patch.object(
                    InstagramScraper,
                    "extract_posts",
                    new=AsyncMock(side_effect=failure),
                ) as extract,
                patch(
                    "zeezazum.platforms.instagram.downloader.MediaDownloader._fetch",
                    new=AsyncMock(return_value=(b"x", "image/jpeg")),
                ),
            ):
                if isinstance(failure, AuthenticationRequired):
                    with self.assertRaises(AuthenticationRequired):
                        await run_scrape(self.cfg, accounts)
                else:
                    await run_scrape(self.cfg, accounts)
                self.assertEqual(extract.await_count, expected_calls)
        self.assertEqual(browser.close.await_count, 2)

    async def test_known_post_url_refresh_preserves_metrics(self):
        from zeezazum.pipeline.orchestrator import _persist_posts
        from datetime import datetime, timezone

        (self.path / "instagram/alice/posts.json").unlink()
        old = Post(
            "abc",
            "url",
            datetime.now(timezone.utc),
            "caption",
            (self.item,),
            metrics={"like_count": 3},
        )
        _persist_posts(self.storage, "instagram/alice", [old])
        newer = Post(
            "abc",
            "url",
            old.timestamp,
            "caption",
            (MediaItem(MediaKind.IMAGE, "https://cdn/new", 0),),
            metrics={"like_count": 9},
        )
        fresh, total = _persist_posts(self.storage, "instagram/alice", [newer])
        saved = self.storage.read_json("instagram/alice/posts.json")[0]
        self.assertFalse(fresh)
        self.assertEqual(total, 1)
        self.assertEqual(saved["media"][0]["url"], "https://cdn/new")
        self.assertEqual(saved["like_count"], 3)

    async def test_cli_auth_exit(self):
        from typer.testing import CliRunner
        from zeezazum.cli import app

        with (
            patch(
                "zeezazum.cli._load_config_and_accounts",
                return_value=(self.cfg, [self.account]),
            ),
            patch(
                "zeezazum.cli.run_scrape",
                new=AsyncMock(side_effect=AuthenticationRequired("renew session")),
            ),
        ):
            result = await asyncio.to_thread(CliRunner().invoke, app, ["scrape"])
        self.assertEqual(result.exit_code, 3)
        self.assertIn("renew session", result.output)

    async def test_backfill_idempotent_and_account_states_independent(self):
        state = StateStore(self.path / "state.duckdb")
        state.upsert(
            CandidateState(
                "1", "alice", "done", newest_post_id="abc", total_posts_seen=1
            )
        )
        state.upsert(CandidateState("1", "bob", "done", newest_post_id="bobpost"))
        queue = MediaQueue(self.cfg, self.storage, state)
        queue.checkpoints = RemoteCheckpoints(self.cfg, self.storage, state, None)
        scraper = AsyncMock()
        scraper.last_profile_stats = {}
        posts = [
            Post("abc", "url", None, None, ()),
            Post("older", "url", None, None, ()),
        ]
        scraper.extract_posts.return_value = posts
        for _ in range(2):
            await _scrape_one(
                account=self.account,
                context=None,
                scraper=scraper,
                storage=self.storage,
                state=state,
                queue=queue,
                max_posts=60,
                since_date=None,
                max_attempts=3,
                backoff_seconds=0,
                extra_posts=1,
            )
        self.assertEqual(len(self.storage.read_json("instagram/alice/posts.json")), 2)
        self.assertEqual(state.get("1", "alice").total_posts_seen, 2)
        self.assertEqual(state.get("1", "bob").newest_post_id, "bobpost")
        self.assertIsNone(scraper.extract_posts.call_args.kwargs["stop_at_post_id"])
        self.assertEqual(scraper.extract_posts.call_args.kwargs["max_posts"], 3)
        await queue.close()
        state.close()

    async def test_worker_limit_and_disabled_video(self):
        state = StateStore(self.path / "state.duckdb")
        self.cfg.media.download_videos = False
        self.storage.write_json(
            "instagram/alice/posts.json",
            [
                {
                    "shortcode": str(i),
                    "media": [{"kind": "image", "index": 0, "url": "url"}],
                }
                for i in range(8)
            ]
            + [
                {
                    "shortcode": "video",
                    "media": [{"kind": "video", "index": 0, "url": "url"}],
                }
            ],
        )
        queue = MediaQueue(self.cfg, self.storage, state)
        active = maximum = 0

        async def fetch(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return b"x", "image/jpeg"

        with patch.object(queue.downloader, "_fetch", side_effect=fetch):
            await queue.reconcile([self.account])
            await queue.close()
        self.assertEqual(queue.downloader.results["downloaded"], 8)
        self.assertLessEqual(maximum, 2)
        self.assertEqual(len(state.pending_media("alice", images=False)), 1)
        self.assertEqual(len(state.pending_media("alice", videos=False)), 0)
        state.close()

    async def test_periodic_failure_is_reported_even_after_retry_succeeds(self):
        state = StateStore(self.path / "state.duckdb")
        remote = LocalStorage(self.path / "remote")
        self.cfg.storage.s3.state_sync_interval_seconds = 0.001
        cp = RemoteCheckpoints(self.cfg, remote, state, self.path / "state.duckdb")
        with patch.object(remote, "write_bytes", side_effect=OSError("offline")):
            await cp.task
        with self.assertRaises(PersistenceFailure):
            cp.check()
        await cp.close()
        state.close()

    async def test_auth_before_scroll(self):
        page = AsyncMock()
        page.on = lambda *args: None
        page.url = "https://instagram.com/accounts/login/"
        context = AsyncMock()
        context.new_page.return_value = page
        with patch(
            "zeezazum.platforms.instagram.scraper.asyncio.sleep", new=AsyncMock()
        ):
            with self.assertRaises(AuthenticationRequired):
                await InstagramScraper().extract_posts(
                    context,
                    "alice",
                    max_posts=60,
                    since_date=None,
                    stop_at_post_id=None,
                )
        page.close.assert_awaited_once()
        page.mouse.wheel.assert_not_awaited()

    async def test_config_requires_positive_limits(self):
        from pydantic import ValidationError

        values = self.cfg.model_dump()
        for section, field in [
            ("media", "max_concurrent_downloads"),
            ("s3", "state_sync_interval_seconds"),
        ]:
            edited = self.cfg.model_dump()
            target = edited["storage"]["s3"] if section == "s3" else edited[section]
            target[field] = 0
            with self.assertRaises(ValidationError):
                ZeezazumConfig.model_validate(edited)
        self.assertEqual(
            ZeezazumConfig.model_validate(
                values
            ).storage.s3.state_sync_interval_seconds,
            60,
        )


if __name__ == "__main__":
    unittest.main()
