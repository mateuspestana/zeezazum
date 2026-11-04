"""Testes do suporte ao TikTok. Nenhum usa rede: o yt-dlp e o navegador são falsos,
e o parser roda sobre entradas reais da listagem de @tiktok (tests/fixtures)."""

import asyncio
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
import typer
from typer.testing import CliRunner
from yt_dlp.utils import DownloadError, ExtractorError

from zeezazum.cli import app
from zeezazum.config import ZeezazumConfig
from zeezazum.input.url_parser import (
    extract_accounts,
    extract_tiktok_accounts,
    extract_tiktok_searches,
    extract_tiktok_tags,
)
from zeezazum.models import (
    CandidateState,
    MediaItem,
    MediaKind,
    Post,
    SocialAccount,
)
from zeezazum.pipeline.orchestrator import (
    MediaQueue,
    PersistenceFailure,
    RemoteCheckpoints,
    _scrape_one,
    run_media_sync,
)
from zeezazum.pipeline.state import StateStore
from zeezazum.platforms import build_scraper
from zeezazum.platforms.base import AuthenticationRequired, ProfileUnavailable
from zeezazum.platforms.instagram.scraper import InstagramScraper
from zeezazum.platforms.tiktok import media as tiktok_media
from zeezazum.platforms.tiktok.parser import parse_entry, parse_item, parse_user_detail
from zeezazum.platforms.tiktok.scraper import CaptchaRequired, TikTokScraper
from zeezazum.platforms.tiktok import session as tiktok_session
from zeezazum.platforms.tiktok.tag_scraper import (
    SCROLL_JS,
    TikTokSearchScraper,
    TikTokTagScraper,
)
from zeezazum.storage.local import LocalStorage

FIXTURE = Path(__file__).parent / "fixtures" / "tiktok_flat_entries.json"


def real_entries():
    return json.loads(FIXTURE.read_text())


def entry(post_id, days_ago=0, user="alice", duration=10, **extra):
    when = datetime(2026, 10, 30, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return {
        "id": str(post_id),
        "url": f"https://www.tiktok.com/@{user}/video/{post_id}",
        "description": f"post {post_id} #tag @amigo",
        "timestamp": int(when.timestamp()),
        "duration": duration,
        "like_count": 1,
        "thumbnails": [{"id": "cover", "url": f"https://cdn/{post_id}.jpg"}],
        **extra,
    }


def config(path, platform="tiktok"):
    return ZeezazumConfig.model_validate(
        {
            "platform": platform,
            "input": {"path": "unused", "url_column": "url", "id_column": "id"},
            "storage": {"local": {"base_path": str(path)}},
            "media": {"max_concurrent_downloads": 2},
        }
    )


class FakeYdl:
    """Imita o YoutubeDL real, inclusive no que importa: com `process=True` (padrão do
    yt-dlp) a playlist é percorrida INTEIRA dentro de `extract_info` e devolvida como
    lista; só com `process=False` as entradas chegam como gerador preguiçoso.

    `script` é uma lista de comportamentos, um por chamada: lista de entradas, None,
    exceção a levantar, ou `(entradas, k)` (falha no k-ésimo elemento da iteração)."""

    calls = 0
    consumed = 0  # quantas entradas o gerador chegou a produzir

    def __init__(self, script):
        self.script = script

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        behavior = self.script[min(FakeYdl.calls, len(self.script) - 1)]
        FakeYdl.calls += 1
        if isinstance(behavior, Exception):
            raise behavior
        if behavior is None:
            return None
        items, fail_at = behavior if isinstance(behavior, tuple) else (behavior, None)

        def gen():
            for i, item in enumerate(items):
                if i == fail_at:
                    raise ExtractorError("falha no meio da paginação")
                FakeYdl.consumed += 1
                yield item

        return {"entries": gen() if not process else list(gen())}


def scraper_with(*script, **kwargs):
    FakeYdl.calls = 0
    FakeYdl.consumed = 0
    return TikTokScraper(
        ydl_factory=lambda: FakeYdl(list(script)),
        retry_delay_range=(0, 0),
        profile_timeout=0.2,
        **kwargs,
    )


def fake_context(udr_scope):
    """Contexto de navegador falso cuja página embute `udr_scope` como dados do perfil."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.close = AsyncMock()
    page.evaluate = AsyncMock(
        return_value=json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": udr_scope}})
    )
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    return context


async def extract(scraper, context=None, **overrides):
    kwargs = dict(max_posts=None, since_date=None, stop_at_post_id=None)
    kwargs.update(overrides)
    return await scraper.extract_posts(context, "alice", **kwargs)


class ParserTests(unittest.TestCase):
    def test_entrada_real_vira_post_com_video_e_capa(self):
        post = parse_entry(real_entries()[0])
        self.assertEqual(post.shortcode, "7702947070604332319")
        self.assertEqual(
            post.post_url, "https://www.tiktok.com/@tiktok/video/7702947070604332319"
        )
        self.assertEqual(post.timestamp, datetime.fromtimestamp(1793482137, tz=timezone.utc))
        self.assertEqual([m.kind for m in post.media], [MediaKind.VIDEO, MediaKind.IMAGE])
        # o vídeo guarda a URL da página (não expira); o yt-dlp resolve o arquivo na hora
        self.assertEqual(post.media[0].url, post.post_url)
        self.assertEqual((post.media[0].index, post.media[1].index), (0, 1))
        self.assertEqual(post.metrics["like_count"], 5789)
        self.assertEqual(post.metrics["media_type_label"], "video")
        self.assertEqual(post.metrics["owner_username"], "tiktok")

    def test_hashtags_e_mencoes_da_legenda(self):
        post = parse_entry(entry(1))
        self.assertEqual(post.metrics["hashtags"], ["tag"])
        self.assertEqual(post.metrics["mentions"], ["amigo"])

    def test_slideshow_sem_duracao_so_enfileira_a_capa(self):
        post = parse_entry(entry(2, duration=None))
        self.assertEqual([m.kind for m in post.media], [MediaKind.IMAGE])
        self.assertEqual(post.media[0].index, 0)
        self.assertEqual(post.metrics["media_type_label"], "photo")

    def test_url_de_foto_nao_enfileira_video(self):
        e = entry(3)
        e["url"] = "https://www.tiktok.com/@alice/photo/3"
        self.assertEqual([m.kind for m in parse_entry(e).media], [MediaKind.IMAGE])

    def test_capa_prefere_origem(self):
        e = entry(4, thumbnails=[
            {"id": "cover", "url": "https://cdn/cover"},
            {"id": "originCover", "url": "https://cdn/origin"},
        ])
        self.assertEqual(parse_entry(e).media[-1].url, "https://cdn/origin")

    def test_entrada_invalida_vira_none(self):
        self.assertIsNone(parse_entry({"id": "9", "url": "https://www.tiktok.com/@a"}))
        self.assertIsNone(parse_entry("lixo"))

    def test_user_detail(self):
        ok = parse_user_detail({"__DEFAULT_SCOPE__": {"webapp.user-detail": {
            "statusCode": 0,
            "userInfo": {
                "user": {"privateAccount": False, "secUid": "SEC"},
                "stats": {"followerCount": 5, "followingCount": 1, "videoCount": 9},
            },
        }}})
        self.assertEqual(ok["posts_total"], 9)
        self.assertEqual(ok["sec_uid"], "SEC")
        gone = parse_user_detail({"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 10221}}})
        self.assertEqual(gone["status_code"], 10221)
        self.assertIsNone(parse_user_detail({"__DEFAULT_SCOPE__": {}}))


class UrlParserTests(unittest.TestCase):
    def handles(self, text):
        return [a.handle for a in extract_tiktok_accounts("1", text)]

    def test_formatos(self):
        self.assertEqual(self.handles("https://www.tiktok.com/@Fulano.Silva"), ["fulano.silva"])
        self.assertEqual(self.handles("tiktok.com/@a_b/video/123456"), ["a_b"])
        self.assertEqual(self.handles("TikTok: @outro"), ["outro"])
        self.assertEqual(self.handles("link tiktok @x.y"), ["x.y"])

    def test_dedup_e_multiplas_contas(self):
        text = "tiktok.com/@um; https://www.tiktok.com/@UM/video/1\ntiktok.com/@dois"
        self.assertEqual(self.handles(text), ["um", "dois"])

    def test_rotulo_exige_separador(self):
        self.assertEqual(self.handles("ttfulano"), [])
        self.assertEqual(self.handles("tiktokfulano"), [])
        self.assertEqual(self.handles("TT: @fulano"), ["fulano"])
        self.assertEqual(self.handles("tiktok @fulano"), ["fulano"])

    def test_ponto_final_nao_faz_parte_do_handle(self):
        self.assertEqual(self.handles("tiktok.com/@abc."), ["abc"])

    def test_url_e_plataforma(self):
        (acc,) = extract_tiktok_accounts("7", "tiktok.com/@abc")
        self.assertEqual((acc.platform, acc.url, acc.row_id), ("tiktok", "https://www.tiktok.com/@abc", "7"))

    def test_link_curto_e_ignorado_com_aviso(self):
        with self.assertLogs("zeezazum.input.url_parser", "WARNING") as logs:
            self.assertEqual(self.handles("https://vm.tiktok.com/ZMabc123/"), [])
        self.assertIn("link curto", logs.output[0])

    def test_nao_mistura_instagram_e_tiktok(self):
        self.assertEqual(self.handles("instagram.com/fulano"), [])
        self.assertEqual(extract_accounts("instagram", "1", "tiktok.com/@fulano"), [])
        self.assertEqual(extract_accounts("tiktok", "1", None), [])
        with self.assertRaises(ValueError):
            extract_accounts("orkut", "1", "x")


class ScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordena_do_mais_novo_mesmo_com_fixado_antigo_no_topo(self):
        pinned_old = entry(900, days_ago=400)
        listing = [pinned_old, entry(3, 0), entry(2, 1), entry(1, 2)]
        posts = await extract(scraper_with(listing))
        self.assertEqual([p.shortcode for p in posts], ["3", "2", "1", "900"])

    async def test_max_posts(self):
        listing = [entry(i, i) for i in range(10, 0, -1)]
        posts = await extract(scraper_with(listing), max_posts=3)
        self.assertEqual(len(posts), 3)

    async def test_stop_at_post_id_para_no_ja_conhecido(self):
        listing = [entry(5, 0), entry(4, 1), entry(3, 2), entry(2, 3)]
        posts = await extract(scraper_with(listing), stop_at_post_id="3")
        self.assertEqual([p.shortcode for p in posts], ["5", "4"])

    async def test_since_date_corta_e_encerra_a_paginacao(self):
        listing = [entry(100 - d, d) for d in range(0, 40)]
        since = datetime(2026, 10, 30, tzinfo=timezone.utc) - timedelta(days=5, hours=1)
        posts = await extract(scraper_with(listing), since_date=since)
        self.assertEqual(len(posts), 6)  # dias 0..5

    async def test_paginacao_e_preguicosa_max_posts_nao_percorre_o_perfil_inteiro(self):
        listing = [entry(i, i % 20) for i in range(5000, 0, -1)]
        posts = await extract(scraper_with(listing), max_posts=3)
        self.assertEqual(len(posts), 3)
        self.assertLessEqual(FakeYdl.consumed, 4)  # não leu as 5000 entradas

    async def test_stop_at_post_id_tambem_para_cedo(self):
        listing = [entry(i, 0) for i in range(5000, 0, -1)]
        await extract(scraper_with(listing), stop_at_post_id="4998")
        self.assertLessEqual(FakeYdl.consumed, 4)

    async def test_falha_do_checkpoint_propaga_sem_virar_falha_de_listagem(self):
        class Boom(Exception):
            pass

        async def on_checkpoint(batch):
            raise Boom("disco cheio")

        listing = [entry(i, 0) for i in range(1, 8)]
        with patch("zeezazum.platforms.tiktok.scraper.CHECKPOINT_EVERY", 3):
            with self.assertRaises(Boom):
                await extract(scraper_with(listing), on_checkpoint=on_checkpoint)
        self.assertEqual(FakeYdl.calls, 1)  # nenhuma repetição da listagem

    async def test_retry_da_abertura_intermitente(self):
        scraper = scraper_with(None, DownloadError("429"), [entry(1)])
        posts = await extract(scraper)
        self.assertEqual([p.shortcode for p in posts], ["1"])
        self.assertEqual(FakeYdl.calls, 3)

    async def test_falha_no_meio_retoma_sem_duplicar_nem_truncar(self):
        items = [entry(i, i) for i in range(5, 0, -1)]
        scraper = scraper_with((items, 3), items)
        posts = await extract(scraper)
        self.assertEqual(sorted(p.shortcode for p in posts), ["1", "2", "3", "4", "5"])

    async def test_lista_incompleta_nao_e_devolvida_como_completa(self):
        items = [entry(i, i) for i in range(5, 0, -1)]
        scraper = scraper_with((items, 2), listing_attempts=3)
        with self.assertRaises(RuntimeError) as ctx:
            await extract(scraper)
        self.assertNotIsInstance(ctx.exception, ProfileUnavailable)
        self.assertIn("2 posts capturados", str(ctx.exception))

    async def test_sem_resposta_e_sem_perfil_verificado_vira_indisponivel(self):
        scraper = scraper_with(DownloadError("Unable to extract secondary user ID"), listing_attempts=3)
        with self.assertRaises(ProfileUnavailable) as ctx:
            await extract(scraper)
        self.assertIn("3 tentativas", str(ctx.exception))
        self.assertEqual(FakeYdl.calls, 3)

    async def test_perfil_existente_mas_bloqueado_e_erro_comum_para_o_orquestrador_repetir(self):
        context = fake_context({"statusCode": 0, "userInfo": {"user": {}, "stats": {"videoCount": 5}}})
        scraper = scraper_with(None, listing_attempts=2)
        with self.assertRaises(RuntimeError) as ctx:
            await extract(scraper, context)
        self.assertNotIsInstance(ctx.exception, ProfileUnavailable)

    async def test_perfil_inexistente_nem_chega_a_listar(self):
        scraper = scraper_with([entry(1)])
        with self.assertRaises(ProfileUnavailable) as ctx:
            await extract(scraper, fake_context({"statusCode": 10221}))
        self.assertIn("não existe", str(ctx.exception))
        self.assertEqual(FakeYdl.calls, 0)

    async def test_perfil_privado(self):
        scraper = scraper_with([entry(1)])
        udr = {"statusCode": 0, "userInfo": {"user": {"privateAccount": True}, "stats": {}}}
        with self.assertRaises(ProfileUnavailable) as ctx:
            await extract(scraper, fake_context(udr))
        self.assertIn("privado", str(ctx.exception))

    async def test_estatisticas_do_perfil_para_o_aviso_de_cobertura(self):
        udr = {"statusCode": 0, "userInfo": {"user": {}, "stats": {
            "followerCount": 10, "followingCount": 2, "videoCount": 77}}}
        scraper = scraper_with([entry(1)])
        await extract(scraper, fake_context(udr))
        self.assertEqual(scraper.last_profile_stats["posts_total"], 77)
        self.assertEqual(scraper.last_profile_stats["followers"], 10)

    async def test_pagina_sem_dados_do_perfil_nao_impede_a_coleta(self):
        context = fake_context({})
        context.new_page.return_value.evaluate = AsyncMock(return_value=None)
        scraper = scraper_with([entry(1)])
        posts = await extract(scraper, context)
        self.assertEqual(len(posts), 1)
        self.assertIsNone(scraper.last_profile_stats)

    async def test_checkpoints_periodicos(self):
        listing = [entry(i, 0) for i in range(1, 8)]
        seen = []

        async def on_checkpoint(batch):
            seen.append(len(batch))

        with patch("zeezazum.platforms.tiktok.scraper.CHECKPOINT_EVERY", 3):
            await extract(scraper_with(listing), on_checkpoint=on_checkpoint)
        self.assertEqual(seen, [3, 6])

    async def test_nao_usa_autenticacao(self):
        # TikTok nunca deve pedir login: falha vira ProfileUnavailable/RuntimeError, nunca
        # AuthenticationRequired (que mandaria o usuário refazer um login que não existe)
        from zeezazum.platforms.base import AuthenticationRequired

        scraper = scraper_with(DownloadError("403"), listing_attempts=1)
        with self.assertRaises(Exception) as ctx:
            await extract(scraper)
        self.assertNotIsInstance(ctx.exception, AuthenticationRequired)

    def test_registro_de_plataformas(self):
        self.assertIsInstance(build_scraper("tiktok"), TikTokScraper)
        self.assertIsInstance(build_scraper("instagram"), InstagramScraper)
        with self.assertRaises(ValueError):
            build_scraper("orkut")


class DownloadVideoTests(unittest.TestCase):
    def fake_ydl(self, outcomes):
        """YoutubeDL falso que grava um arquivo no outtmpl (ou levanta)."""
        calls = []

        class Ydl:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                calls.append((url, self.options["format"]))
                outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
                if isinstance(outcome, Exception):
                    raise outcome
                path = self.options["outtmpl"].replace("%(id)s", "1").replace("%(ext)s", "mp4")
                Path(path).write_bytes(outcome)

        return Ydl, calls

    def test_baixa_e_devolve_os_bytes(self):
        Ydl, calls = self.fake_ydl([b"MP4DATA"])
        with patch.object(tiktok_media.yt_dlp, "YoutubeDL", Ydl):
            data = tiktok_media.download_video("https://www.tiktok.com/@a/video/1", video_format="fmt")
        self.assertEqual(data, b"MP4DATA")
        self.assertEqual(calls, [("https://www.tiktok.com/@a/video/1", "fmt")])

    def test_tenta_de_novo_na_falha_intermitente(self):
        Ydl, calls = self.fake_ydl([DownloadError("429"), b"OK"])
        with patch.object(tiktok_media.yt_dlp, "YoutubeDL", Ydl), patch.object(tiktok_media.time, "sleep"):
            self.assertEqual(tiktok_media.download_video("u"), b"OK")
        self.assertEqual(len(calls), 2)

    def test_falha_depois_de_todas_as_tentativas(self):
        Ydl, calls = self.fake_ydl([DownloadError("429")])
        with patch.object(tiktok_media.yt_dlp, "YoutubeDL", Ydl), patch.object(tiktok_media.time, "sleep"):
            with self.assertRaises(RuntimeError):
                tiktok_media.download_video("u", attempts=3)
        self.assertEqual(len(calls), 3)


class PlatformStateTests(unittest.TestCase):
    def test_mesmo_handle_nas_duas_plataformas_nao_se_mistura(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "s.duckdb")
            state.upsert(CandidateState("1", "alice", "done", newest_post_id="ig"))
            state.upsert(CandidateState("1", "alice", "done", newest_post_id="tt", platform="tiktok"))
            self.assertEqual(state.get("1", "alice").newest_post_id, "ig")
            self.assertEqual(state.get("1", "alice", "tiktok").newest_post_id, "tt")

            item = MediaItem(MediaKind.VIDEO, "u", 0)
            state.register_media("alice", "p", item)
            state.register_media("alice", "p", item, platform="tiktok")
            self.assertEqual(len(state.pending_media("alice")), 1)
            self.assertEqual(len(state.pending_media("alice", platform="tiktok")), 1)

            state.finish_media("alice", "p", 0, platform="tiktok")
            self.assertEqual(state.pending_media("alice", platform="tiktok"), [])
            self.assertEqual(len(state.pending_media("alice")), 1)  # Instagram intacto

            state.refresh_pending()
            self.assertEqual(state.get("1", "alice").media_pending, 1)
            self.assertEqual(state.get("1", "alice", "tiktok").media_pending, 0)
            state.close()


class TikTokPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.cfg = config(self.path)
        self.storage = LocalStorage(self.path)
        self.account = SocialAccount("1", "tiktok", "alice", "https://www.tiktok.com/@alice")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def run_one(self, scraper, state, queue, **overrides):
        kwargs = dict(
            account=self.account, context=None, scraper=scraper, storage=self.storage,
            state=state, queue=queue, max_posts=60, since_date=None,
            max_attempts=1, backoff_seconds=0,
        )
        kwargs.update(overrides)
        await _scrape_one(**kwargs)

    async def test_coleta_grava_em_tiktok_baixa_video_com_ytdlp_e_capa_por_http(self):
        state = StateStore(self.path / "state.duckdb")
        queue = MediaQueue(self.cfg, self.storage, state)
        queue.checkpoints = RemoteCheckpoints(self.cfg, self.storage, state, None)
        scraper = AsyncMock()
        scraper.last_profile_stats = {"posts_total": 1}
        scraper.extract_posts.return_value = [parse_entry(entry(11))]
        download = MagicMock(return_value=b"VIDEO")
        fetch = AsyncMock(return_value=(b"COVER", "image/jpeg"))
        with (
            patch("zeezazum.platforms.downloader.tiktok_media.download_video", download),
            patch.object(queue.downloader, "_fetch", new=fetch),
        ):
            await self.run_one(scraper, state, queue)
            await queue.close()

        self.assertEqual(self.storage.read_bytes("tiktok/alice/media/11_0.mp4"), b"VIDEO")
        self.assertEqual(self.storage.read_bytes("tiktok/alice/media/11_1.jpg"), b"COVER")
        download.assert_called_once()
        self.assertEqual(download.call_args.args[0], "https://www.tiktok.com/@alice/video/11")
        self.assertEqual(download.call_args.kwargs["video_format"], "best")
        fetch.assert_awaited_once()  # só a capa passa pelo HTTP direto
        self.assertEqual(state.get("1", "alice", "tiktok").status, "done")
        self.assertIsNone(state.get("1", "alice"))  # nada vazou para o estado do Instagram
        self.assertEqual(state.pending_media("alice", platform="tiktok"), [])
        posts = self.storage.read_json("tiktok/alice/posts.json")
        self.assertEqual(posts[0]["like_count"], 1)
        self.assertEqual(self.storage.read_json("tiktok/alice/profile.json")["posts_total"], 1)
        state.close()

    async def test_falha_de_download_fica_pendente_e_media_sync_recupera(self):
        state = StateStore(self.path / "state.duckdb")
        queue = MediaQueue(self.cfg, self.storage, state)
        queue.checkpoints = RemoteCheckpoints(self.cfg, self.storage, state, None)
        scraper = AsyncMock()
        scraper.last_profile_stats = None
        scraper.extract_posts.return_value = [parse_entry(entry(12, duration=None))]  # só capa
        with patch.object(queue.downloader, "_fetch", new=AsyncMock(side_effect=OSError("offline"))):
            await self.run_one(scraper, state, queue)
            await queue.close()
        self.assertEqual(len(state.pending_media("alice", platform="tiktok")), 1)
        self.assertEqual(state.get("1", "alice", "tiktok").media_pending, 1)
        state.close()

        fetch = AsyncMock(return_value=(b"COVER", "image/jpeg"))
        with patch("zeezazum.platforms.downloader.MediaDownloader._fetch", new=fetch):
            queued = await run_media_sync(self.cfg, [self.account])
        self.assertEqual(queued, {"alice": 1})
        self.assertEqual(self.storage.read_bytes("tiktok/alice/media/12_0.jpg"), b"COVER")

    async def test_video_que_falha_no_ytdlp_conta_como_falha(self):
        self.storage.write_json("tiktok/alice/posts.json", [{
            "shortcode": "13",
            "media": [{"kind": "video", "index": 0, "url": "https://www.tiktok.com/@alice/video/13"}],
        }])
        with patch(
            "zeezazum.platforms.downloader.tiktok_media.download_video",
            side_effect=RuntimeError("429"),
        ):
            with self.assertRaises(PersistenceFailure):
                await run_media_sync(self.cfg, [self.account])
        state = StateStore(self.path / "_state.duckdb")
        self.assertEqual(len(state.pending_media("alice", platform="tiktok")), 1)
        state.close()


class ConfigAndCliTests(unittest.TestCase):
    def test_config_aceita_tiktok_e_rejeita_outras(self):
        self.assertEqual(config(Path("/tmp/x")).platform, "tiktok")
        with self.assertRaises(Exception):
            config(Path("/tmp/x"), platform="orkut")

    def test_prepare_tiktok(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.csv"
            pl.DataFrame({
                "id": ["1", "2", "3"],
                "redes": ["tiktok.com/@um; instagram.com/foo", "TikTok: @dois", "sem rede"],
            }).write_csv(src)
            out = Path(tmp) / "out.csv"
            result = CliRunner().invoke(app, [
                "prepare", "--input", str(src), "--column", "redes",
                "--id-column", "id", "--platform", "tiktok", "--output", str(out),
            ])
            self.assertEqual(result.exit_code, 0, result.output)
            table = pl.read_csv(out)
            self.assertEqual(table.columns, ["row_id", "handle", "tiktok_url"])
            self.assertEqual(table["handle"].to_list(), ["um", "dois"])
            self.assertIn("2 contas de tiktok", result.output)

    def test_prepare_rejeita_plataforma_desconhecida(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.csv"
            pl.DataFrame({"id": ["1"], "r": ["x"]}).write_csv(src)
            result = CliRunner().invoke(app, [
                "prepare", "--input", str(src), "--column", "r", "--id-column", "id",
                "--platform", "orkut", "--output", str(Path(tmp) / "o.csv"),
            ])
            self.assertNotEqual(result.exit_code, 0)


def captcha_context(captcha, udr_scope=None):
    """Contexto falso: a página embute o perfil e mostra (ou não) o captcha."""
    udr = json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": udr_scope or {}}})

    async def evaluate(script):
        return captcha if script == tiktok_session.CAPTCHA_JS else udr

    context = fake_context(udr_scope or {})
    context.new_page.return_value.evaluate = AsyncMock(side_effect=evaluate)
    return context


EXISTE = {"statusCode": 0, "userInfo": {"user": {}, "stats": {"videoCount": 5}}}


class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_listagem_esgotada_com_captcha_pede_resolucao_manual(self):
        scraper = scraper_with(None, listing_attempts=2, captcha_timeout=0)
        with self.assertRaises(CaptchaRequired) as ctx:
            await extract(scraper, captcha_context(True, EXISTE))
        self.assertIsInstance(ctx.exception, AuthenticationRequired)  # para o lote, exit 3
        self.assertIn("tiktok-session", str(ctx.exception))
        self.assertIn("@alice", str(ctx.exception))

    async def test_sem_captcha_na_tela_continua_sendo_erro_comum(self):
        scraper = scraper_with(None, listing_attempts=2, captcha_timeout=0)
        with self.assertRaises(RuntimeError) as ctx:
            await extract(scraper, captcha_context(False, EXISTE))
        self.assertNotIsInstance(ctx.exception, AuthenticationRequired)

    async def test_captcha_na_tela_mas_listagem_funcionou_nao_incomoda_ninguem(self):
        # o captcha aparece em quase toda abertura anônima; só importa se a lista falhar
        scraper = scraper_with([entry(1), entry(2)])
        posts = await extract(scraper, captcha_context(True, EXISTE))
        self.assertEqual(len(posts), 2)

    async def test_lista_parcial_nao_vira_pedido_de_captcha(self):
        items = [entry(i, i) for i in range(5, 0, -1)]
        scraper = scraper_with((items, 2), listing_attempts=2, captcha_timeout=0)
        with self.assertRaises(RuntimeError) as ctx:
            await extract(scraper, captcha_context(True, EXISTE))
        self.assertNotIsInstance(ctx.exception, AuthenticationRequired)

    async def test_mensagem_muda_quando_ja_existe_sessao_salva(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "tt.json"
            state.write_text(json.dumps({"cookies": []}))
            scraper = scraper_with(None, listing_attempts=1, captcha_timeout=0, session_file=state)
            with self.assertRaises(CaptchaRequired) as ctx:
                await extract(scraper, captcha_context(True, EXISTE))
        self.assertIn("não bastou", str(ctx.exception))


class SessionTests(unittest.TestCase):
    STATE = {
        "cookies": [
            {"name": "ttwid", "value": "abc", "domain": ".tiktok.com", "path": "/",
             "expires": 1900000000.5, "httpOnly": True, "secure": True},
            {"name": "msToken", "value": "xyz", "domain": "www.tiktok.com", "path": "/",
             "expires": -1, "httpOnly": False, "secure": False},
            {"name": "outro", "value": "1", "domain": ".example.com", "path": "/", "expires": -1},
        ],
        "origins": [],
    }

    def test_storage_state_vira_cookies_netscape(self):
        lines = tiktok_session.storage_state_to_netscape(self.STATE).splitlines()
        self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
        self.assertEqual(lines[1], "#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t1900000000\tttwid\tabc")
        self.assertEqual(lines[2], "www.tiktok.com\tFALSE\t/\tFALSE\t0\tmsToken\txyz")
        self.assertEqual(len(lines), 3)  # cookie de outro domínio não entra

    def test_yt_dlp_consegue_ler_o_arquivo_gerado(self):
        from http.cookiejar import MozillaCookieJar

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt"
            path.write_text(tiktok_session.storage_state_to_netscape(self.STATE))
            from yt_dlp.cookies import YoutubeDLCookieJar

            jar = YoutubeDLCookieJar(str(path))
            jar.load(ignore_discard=True, ignore_expires=True)
            self.assertEqual({c.name: c.value for c in jar}, {"ttwid": "abc", "msToken": "xyz"})

    def _poll(self, states, closed=False, **kw):
        """`states`: lista de (captcha_na_tela, links_de_video), um por verificação."""
        steps = list(states)
        idx = [0]

        current = [steps[0]]

        def evaluate(script):
            if script == tiktok_session.CAPTCHA_JS:  # toda verificação começa por aqui
                current[0] = steps[min(idx[0], len(steps) - 1)]
                idx[0] += 1
                return current[0][0]
            return current[0][1]

        page = MagicMock()
        page.evaluate = MagicMock(side_effect=evaluate)
        page.is_closed = MagicMock(return_value=closed)
        t = [0.0]

        def sleep(_):
            t[0] += 1

        ok = tiktok_session.wait_until_solved(
            page, sleep=sleep, clock=lambda: t[0], echo=lambda *_: None, **kw
        )
        return ok, idx[0], t[0]

    def test_resolvido_so_com_captcha_fora_e_videos_na_tela_por_3_checagens(self):
        ok, checks, _ = self._poll([(True, 0), (False, 5), (False, 5), (False, 5)])
        self.assertTrue(ok)
        self.assertEqual(checks, 4)

    def test_fechar_o_modal_sem_resolver_nao_conta(self):
        # o captcha some, mas a página segue sem vídeos: não é sessão válida
        ok, _, elapsed = self._poll([(False, 0)], timeout=20)
        self.assertFalse(ok)
        self.assertEqual(elapsed, 20)

    def test_captcha_que_volta_zera_a_contagem(self):
        ok, checks, _ = self._poll([(False, 5), (False, 5), (True, 0), (False, 5), (False, 5), (False, 5)])
        self.assertTrue(ok)
        self.assertEqual(checks, 6)

    def test_janela_fechada_encerra_a_espera_sem_aprovar(self):
        page = MagicMock()
        page.evaluate = MagicMock(side_effect=Exception("closed"))
        page.is_closed = MagicMock(return_value=True)
        ok = tiktok_session.wait_until_solved(page, sleep=lambda _: None, echo=lambda *_: None)
        self.assertFalse(ok)

    def test_scraper_entrega_os_cookies_da_sessao_ao_yt_dlp(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "tt.json"
            state.write_text(json.dumps(self.STATE))
            scraper = TikTokScraper(session_file=state)
            ydl = scraper._ydl_factory()
            cookiefile = ydl.params["cookiefile"]
            self.assertIn("ttwid", Path(cookiefile).read_text())
            ydl.close()

    def test_sem_sessao_o_yt_dlp_segue_anonimo(self):
        ydl = TikTokScraper(session_file="/nao/existe.json")._ydl_factory()
        self.assertNotIn("cookiefile", ydl.params)
        ydl.close()

    def test_sessao_corrompida_nao_derruba_a_coleta(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "tt.json"
            state.write_text("{lixo")
            ydl = TikTokScraper(session_file=state)._ydl_factory()
            self.assertNotIn("cookiefile", ydl.params)
            ydl.close()

    def test_orquestrador_so_usa_sessao_que_existe(self):
        from zeezazum.pipeline.orchestrator import _session_file

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            cfg.scraping.tiktok_session_file = str(Path(tmp) / "tt.json")
            self.assertIsNone(_session_file(cfg, "tiktok"))
            Path(cfg.scraping.tiktok_session_file).write_text("{}")
            self.assertEqual(_session_file(cfg, "tiktok"), Path(cfg.scraping.tiktok_session_file))
            cfg.scraping.session_file = str(Path(tmp) / "ig.json")
            self.assertIsNone(_session_file(cfg, "instagram"))  # login ainda não feito
            Path(cfg.scraping.session_file).write_text("{}")
            self.assertEqual(_session_file(cfg, "instagram"), Path(cfg.scraping.session_file))

    def test_cli_para_o_lote_com_codigo_3_e_mostra_a_orientacao(self):
        from zeezazum.cli import _execute

        async def blocked():
            raise CaptchaRequired("rode tiktok-session")

        with self.assertRaises(typer.Exit) as ctx:
            _execute(blocked())
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_comando_tiktok_session_salva_no_caminho_do_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                "input: {path: x, url_column: u, id_column: i}\n"
                f"scraping: {{tiktok_session_file: {tmp}/s/tt.json}}\n"
            )
            with patch("zeezazum.platforms.tiktok.session.capture_session", return_value=True) as cap:
                result = CliRunner().invoke(app, ["tiktok-session", "--config", str(cfg)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(cap.call_args.args[0], Path(f"{tmp}/s/tt.json"))

    def test_comando_abre_a_busca_e_a_tag_pedidas_na_mesma_sessao(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "tt.json")
            for args, urls in (
                (["--search", "jair bolsonaro"], ["https://www.tiktok.com/search/video?q=jair+bolsonaro"]),
                (["--tag", "#Lula"], ["https://www.tiktok.com/tag/Lula"]),
                (["--tag", "a", "--search", "b"],
                 ["https://www.tiktok.com/tag/a", "https://www.tiktok.com/search/video?q=b"]),
            ):
                with patch("zeezazum.platforms.tiktok.session.capture_session", return_value=True) as cap:
                    result = CliRunner().invoke(app, ["tiktok-session", "--output", out, *args])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(cap.call_args.kwargs["urls"], urls)

    def test_comando_falha_e_avisa_que_a_sessao_existente_foi_mantida(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("zeezazum.platforms.tiktok.session.capture_session", return_value=False):
                result = CliRunner().invoke(
                    app, ["tiktok-session", "--output", str(Path(tmp) / "tt.json")]
                )
            self.assertEqual(result.exit_code, 1)
            self.assertIn("foi mantida", result.output)
            self.assertNotIn("Sessão salva", result.output)


def web_item(post_id, days_ago=0, user="bia", **extra):
    """Item no formato da API web (`/api/challenge/item_list/`)."""
    when = datetime(2026, 10, 30, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return {
        "id": str(post_id),
        "desc": f"post {post_id} #bolsonaro #lula @amigo",
        "createTime": str(int(when.timestamp())),
        "author": {"uniqueId": user, "id": "777"},
        "stats": {"diggCount": 5, "commentCount": 2, "playCount": 90, "shareCount": 1, "collectCount": 3},
        "video": {"duration": 12, "cover": "https://cdn/c.jpg", "originCover": "https://cdn/o.jpg"},
        "music": {"title": "som", "authorName": "dj"},
        "textExtra": [{"hashtagName": "bolsonaro"}, {"hashtagName": "lula"}],
        **extra,
    }


class TagParserTests(unittest.TestCase):
    def test_item_de_video(self):
        post = parse_item(web_item(42))
        self.assertEqual(post.shortcode, "42")
        self.assertEqual(post.post_url, "https://www.tiktok.com/@bia/video/42")
        self.assertEqual([(m.kind, m.url) for m in post.media], [
            (MediaKind.VIDEO, "https://www.tiktok.com/@bia/video/42"),
            (MediaKind.IMAGE, "https://cdn/o.jpg"),  # capa original tem preferência
        ])
        m = post.metrics
        self.assertEqual((m["like_count"], m["comment_count"], m["view_count"]), (5, 2, 90))
        self.assertEqual((m["share_count"], m["save_count"], m["video_duration"]), (1, 3, 12))
        self.assertEqual(m["hashtags"], ["bolsonaro", "lula"])
        self.assertEqual((m["owner_username"], m["owner_id"], m["audio_artist"]), ("bia", "777", "dj"))
        self.assertEqual(post.timestamp.year, 2026)

    def test_slideshow_baixa_as_fotos_e_nao_enfileira_video(self):
        imgs = {"images": [{"imageURL": {"urlList": ["https://cdn/1.jpg"]}},
                           {"imageURL": {"urlList": ["https://cdn/2.jpg", "x"]}}]}
        post = parse_item(web_item(7, imagePost=imgs))
        self.assertIn("/photo/7", post.post_url)
        self.assertEqual([(m.kind, m.url, m.index) for m in post.media],
                         [(MediaKind.IMAGE, "https://cdn/1.jpg", 0), (MediaKind.IMAGE, "https://cdn/2.jpg", 1)])
        self.assertEqual(post.metrics["media_type_label"], "photo")

    def test_autor_como_texto_e_hashtags_da_legenda(self):
        item = web_item(9, author="carlos")
        del item["textExtra"]
        post = parse_item(item)
        self.assertEqual(post.metrics["owner_username"], "carlos")
        self.assertEqual(post.metrics["hashtags"], ["bolsonaro", "lula"])

    def test_live_e_lixo_viram_none(self):
        self.assertIsNone(parse_item(web_item(1, liveRoomInfo={"x": 1})))
        self.assertIsNone(parse_item(web_item("abc")))
        self.assertIsNone(parse_item({"id": "5"}))  # sem autor
        self.assertIsNone(parse_item("lixo"))


class TagInputTests(unittest.TestCase):
    def handles(self, text):
        return [a.handle for a in extract_tiktok_tags("1", text)]

    def test_formatos(self):
        self.assertEqual(self.handles("https://www.tiktok.com/tag/Bolsonaro"), ["bolsonaro"])
        self.assertEqual(self.handles("#Lula"), ["lula"])
        self.assertEqual(self.handles("eleicoes2026"), ["eleicoes2026"])

    def test_varias_separadas_e_deduplicadas(self):
        self.assertEqual(
            self.handles("#bolsonaro, #Lula; tiktok.com/tag/bolsonaro\n#ciro"),
            ["bolsonaro", "lula", "ciro"],
        )

    def test_unicode_e_url_codificada(self):
        self.assertEqual(self.handles("https://www.tiktok.com/tag/elei%C3%A7%C3%A3o"), ["eleição"])
        acc = extract_tiktok_tags("1", "#eleição")[0]
        self.assertEqual(acc.url, "https://www.tiktok.com/tag/elei%C3%A7%C3%A3o")
        self.assertEqual(acc.platform, "tiktok_tag")

    def test_perfil_e_video_nao_viram_tag(self):
        self.assertEqual(self.handles("https://www.tiktok.com/@fulano"), [])
        self.assertEqual(self.handles("https://www.tiktok.com/@fulano/video/123"), [])
        self.assertEqual(self.handles(""), [])
        self.assertEqual(self.handles(None), [])

    def test_despacho_e_registro(self):
        self.assertEqual(extract_accounts("tiktok_tag", "1", "#a")[0].platform, "tiktok_tag")
        from zeezazum.platforms import PLATFORMS, REQUIRES_LOGIN

        self.assertIn("tiktok_tag", PLATFORMS)
        self.assertFalse(REQUIRES_LOGIN["tiktok_tag"])
        self.assertIsInstance(build_scraper("tiktok_tag"), TikTokTagScraper)
        self.assertEqual(config(".", "tiktok_tag").platform, "tiktok_tag")

    def test_prepare_tiktok_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.csv"
            pl.DataFrame({"id": ["1", "2"], "t": ["#bolsonaro #lula", "tiktok.com/tag/ciro"]}).write_csv(src)
            out = Path(tmp) / "o.csv"
            result = CliRunner().invoke(app, [
                "prepare", "--input", str(src), "--column", "t", "--id-column", "id",
                "--platform", "tiktok_tag", "--output", str(out),
            ])
            self.assertEqual(result.exit_code, 0, result.output)
            df = pl.read_csv(out)
            self.assertEqual(df["handle"].to_list(), ["bolsonaro", "lula", "ciro"])
            self.assertIn("tiktok_tag_url", df.columns)


TAG_API = "https://www.tiktok.com/api/challenge/item_list/?x=1"
SEARCH_API = "https://www.tiktok.com/api/search/item/full/?x=1"


class FakeResponse:
    def __init__(self, payload, url=TAG_API):
        self.url, self._payload = url, payload

    async def json(self):
        if self._payload is None:
            raise ValueError("corpo vazio")  # 200 com 0 bytes
        return self._payload


class FakeTagPage:
    """Entrega o lote 0 no `goto` e um lote novo a cada rolagem, pelo listener de
    `response` que o scraper registrou (como o navegador faria)."""

    def __init__(self, batches, captcha=False, finish_flag=False, search=False):
        self.search = search  # busca: `item_list` + `has_more` (0/1); tag: `itemList` + `hasMore`
        self.finish_flag = finish_flag  # True: o último lote traz hasMore=False
        self.batches, self.captcha, self.sent, self.scrolls = list(batches), captcha, 0, 0
        self.handler = None
        self.close = AsyncMock()

    def on(self, event, handler):
        assert event == "response"
        self.handler = handler

    async def _deliver(self):
        if self.sent < len(self.batches):
            batch, self.sent = self.batches[self.sent], self.sent + 1
            more = self.sent < len(self.batches) or not self.finish_flag
            if batch is None:
                payload = None
            elif self.search:
                payload = {"item_list": batch, "has_more": int(more), "cursor": 12 * self.sent}
            else:
                payload = {"itemList": batch, "hasMore": more}
            await self.handler(FakeResponse(payload, url=SEARCH_API if self.search else TAG_API))
            await self.handler(FakeResponse({"itemList": [web_item(999)]}, url="https://x/api/other/"))

    async def goto(self, url, **kw):
        self.url = url
        await self._deliver()

    async def evaluate(self, script):
        from zeezazum.platforms.tiktok.tag_scraper import SCROLL_JS

        if script == SCROLL_JS:  # rolagem por JS (mouse.wheel é travado pelo modal de login)
            self.scrolls += 1
            await self._deliver()
            return None
        return self.captcha


def tag_context(page):
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    return context


def tag_scraper(**kw):
    kw.setdefault("scroll_delay", 0)
    kw.setdefault("first_load_timeout", 0)
    return TikTokTagScraper(**kw)


async def extract_tag(scraper, page, handle="bolsonaro", **overrides):
    kwargs = dict(max_posts=None, since_date=None, stop_at_post_id=None)
    kwargs.update(overrides)
    return await scraper.extract_posts(tag_context(page), handle, **kwargs)


class TagScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_rola_captura_e_ordena_do_mais_novo(self):
        page = FakeTagPage([[web_item(1, 5), web_item(2, 1)], [web_item(3, 3), web_item(2, 1)]])
        posts = await extract_tag(tag_scraper(max_stagnant_scrolls=2), page)
        self.assertEqual([p.shortcode for p in posts], ["2", "3", "1"])  # sem duplicar, sem o payload de outra URL
        self.assertEqual(page.url, "https://www.tiktok.com/tag/bolsonaro")

    async def test_para_quando_a_pagina_deixa_de_trazer_posts_novos(self):
        page = FakeTagPage([[web_item(1)], [web_item(2)]])
        await extract_tag(tag_scraper(max_stagnant_scrolls=3), page)
        # 1 rolagem traz o 2º lote, 2 rolagens seguem sem novidade; na 3ª estagnação para sem rolar
        self.assertEqual(page.scrolls, 4)

    async def test_para_assim_que_o_tiktok_avisa_o_fim_da_lista(self):
        page = FakeTagPage([[web_item(1)], [web_item(2)], [web_item(3)]], finish_flag=True)
        posts = await extract_tag(tag_scraper(max_stagnant_scrolls=50), page)
        self.assertEqual(len(posts), 3)
        self.assertEqual(page.scrolls, 2)  # não gasta as 50 rolagens de estagnação

    async def test_rola_por_js_e_nunca_pela_roda_do_mouse(self):
        page = FakeTagPage([[web_item(1)], [web_item(2)]], finish_flag=True)
        page.mouse = MagicMock()
        await extract_tag(tag_scraper(max_stagnant_scrolls=3), page)
        page.mouse.wheel.assert_not_called()

    async def test_max_posts_para_a_rolagem(self):
        page = FakeTagPage([[web_item(i, i)] for i in range(1, 20)])
        posts = await extract_tag(tag_scraper(max_stagnant_scrolls=5), page, max_posts=3)
        self.assertEqual(len(posts), 3)
        self.assertLess(page.scrolls, 6)

    async def test_since_date_filtra_no_final_sem_cortar_a_rolagem_antes(self):
        # a lista da tag não é cronológica: um post antigo no meio não encerra a coleta
        page = FakeTagPage([[web_item(1, 100)], [web_item(2, 1)], [web_item(3, 2)]])
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        posts = await extract_tag(tag_scraper(max_stagnant_scrolls=2), page, since_date=since)
        self.assertEqual([p.shortcode for p in posts], ["2", "3"])

    async def test_stop_at_post_id_e_ignorado(self):
        page = FakeTagPage([[web_item(1, 1), web_item(2, 2), web_item(3, 3)]])
        posts = await extract_tag(tag_scraper(max_stagnant_scrolls=1), page, stop_at_post_id="1")
        self.assertEqual(len(posts), 3)

    async def test_sem_posts_e_com_captcha_pede_resolucao_manual(self):
        page = FakeTagPage([None], captcha=True)  # item_list com 0 bytes
        with self.assertRaises(CaptchaRequired) as ctx:
            await extract_tag(tag_scraper(max_stagnant_scrolls=1), page)
        self.assertIn("tiktok-session", str(ctx.exception))
        self.assertIn("#bolsonaro", str(ctx.exception))
        self.assertIn("necessária", str(ctx.exception))
        page.close.assert_awaited()

    async def test_com_sessao_salva_a_mensagem_diz_que_nao_bastou(self):
        page = FakeTagPage([None], captcha=True)
        with self.assertRaises(CaptchaRequired) as ctx:
            await extract_tag(tag_scraper(max_stagnant_scrolls=1, session_file="s/tt.json"), page)
        self.assertIn("não bastou", str(ctx.exception))

    async def test_sem_posts_e_sem_captcha_devolve_lista_vazia(self):
        page = FakeTagPage([[]], captcha=False)
        self.assertEqual(await extract_tag(tag_scraper(max_stagnant_scrolls=1), page), [])

    async def test_checkpoints_periodicos(self):
        page = FakeTagPage([[web_item(i, i) for i in range(1, 8)]])
        seen = []

        async def on_checkpoint(batch):
            seen.append(len(batch))

        with patch("zeezazum.platforms.tiktok.tag_scraper.CHECKPOINT_EVERY", 3):
            await extract_tag(tag_scraper(max_stagnant_scrolls=1), page, on_checkpoint=on_checkpoint)
        self.assertEqual(seen, [7])


class SearchInputTests(unittest.TestCase):
    def pairs(self, text):
        return [(a.handle, a.url) for a in extract_tiktok_searches("1", text)]

    def test_todos_os_formatos_viram_a_mesma_busca(self):
        want = [("jair+bolsonaro", "https://www.tiktok.com/search/video?q=jair+bolsonaro")]
        for text in (
            "jair bolsonaro",
            "Jair  Bolsonaro",
            "https://www.tiktok.com/search/video?q=jair%20bolsonaro",
            "tiktok.com/search?q=Jair+Bolsonaro&t=123",
            "tiktok.com/search/jair+bolsonaro",
            "https://www.tiktok.com/search/video/jair+bolsonaro",
        ):
            self.assertEqual(self.pairs(text), want, text)

    def test_varias_buscas_por_linha_ou_ponto_e_virgula_e_deduplicadas(self):
        self.assertEqual(
            [h for h, _ in self.pairs("lula; jair bolsonaro\nLULA\n")], ["lula", "jair+bolsonaro"]
        )

    def test_caracteres_reservados_nao_quebram_o_caminho_da_pasta(self):
        (handle, url), = self.pairs("c++ e a/b")
        self.assertEqual(handle, "c%2B%2B+e+a%2Fb")
        self.assertNotIn("/", handle)
        self.assertEqual(url, "https://www.tiktok.com/search/video?q=c%2B%2B+e+a%2Fb")

    def test_acentos_ficam_legiveis(self):
        self.assertEqual([h for h, _ in self.pairs("eleição 2026")], ["eleição+2026"])

    def test_links_que_nao_sao_busca_e_vazios_sao_ignorados(self):
        for text in ("https://www.tiktok.com/@fulano", "https://www.tiktok.com/tag/lula",
                     "tiktok.com/search", "tiktok.com/search/video", "", None, "  ; \n "):
            self.assertEqual(self.pairs(text), [], text)

    def test_despacho_registro_e_prepare(self):
        from zeezazum.platforms import DISPLAY_NAMES, PLATFORMS, REQUIRES_LOGIN, TIKTOK_PLATFORMS

        self.assertEqual(extract_accounts("tiktok_search", "1", "lula")[0].platform, "tiktok_search")
        self.assertIn("tiktok_search", PLATFORMS)
        self.assertIn("tiktok_search", TIKTOK_PLATFORMS)
        self.assertFalse(REQUIRES_LOGIN["tiktok_search"])
        self.assertIn("buscas", DISPLAY_NAMES["tiktok_search"])
        self.assertIsInstance(build_scraper("tiktok_search"), TikTokSearchScraper)
        self.assertEqual(config(".", "tiktok_search").platform, "tiktok_search")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.csv"
            pl.DataFrame({"id": ["1"], "q": ["jair bolsonaro; lula"]}).write_csv(src)
            out = Path(tmp) / "o.csv"
            result = CliRunner().invoke(app, [
                "prepare", "--input", str(src), "--column", "q", "--id-column", "id",
                "--platform", "tiktok_search", "--output", str(out),
            ])
            self.assertEqual(result.exit_code, 0, result.output)
            df = pl.read_csv(out)
            self.assertEqual(df["handle"].to_list(), ["jair+bolsonaro", "lula"])
            self.assertIn("tiktok_search_url", df.columns)


class SearchScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_abre_a_url_da_aba_de_videos_e_le_item_list(self):
        scraper = TikTokSearchScraper(scroll_delay=0, first_load_timeout=0, max_stagnant_scrolls=2)
        page = FakeTagPage([[web_item(1, 3), web_item(2, 1)]], search=True, finish_flag=True)
        posts = await extract_tag(scraper, page, handle="jair+bolsonaro")
        self.assertEqual(page.url, "https://www.tiktok.com/search/video?q=jair+bolsonaro")
        self.assertEqual([p.shortcode for p in posts], ["2", "1"])

    async def test_has_more_zero_encerra_a_coleta(self):
        scraper = TikTokSearchScraper(scroll_delay=0, first_load_timeout=0, max_stagnant_scrolls=50)
        page = FakeTagPage([[web_item(1)], [web_item(2)], [web_item(3)]], search=True, finish_flag=True)
        posts = await extract_tag(scraper, page, handle="lula")
        self.assertEqual((len(posts), page.scrolls), (3, 2))

    async def test_scraper_de_tag_ignora_respostas_da_busca(self):
        page = FakeTagPage([[web_item(1)]], search=True)
        posts = await extract_tag(tag_scraper(max_stagnant_scrolls=1), page)
        self.assertEqual(posts, [])

    async def test_sem_resultado_e_com_captcha_orienta_o_comando_da_busca(self):
        scraper = TikTokSearchScraper(scroll_delay=0, first_load_timeout=0, max_stagnant_scrolls=1)
        page = FakeTagPage([None], captcha=True, search=True)
        with self.assertRaises(CaptchaRequired) as ctx:
            await extract_tag(scraper, page, handle="jair+bolsonaro")
        self.assertIn('--search "jair bolsonaro"', str(ctx.exception))
        self.assertIn("busca «jair bolsonaro»", str(ctx.exception))

    def test_anuncios_nao_viram_posts(self):
        self.assertIsNone(parse_item(web_item(5, isAd=True)))
        self.assertIsNotNone(parse_item(web_item(5, isAd=False)))

    def test_rolagem_traz_o_ultimo_video_para_a_vista(self):
        # na busca o documento não rola; só scrollIntoView do último vídeo dispara as páginas
        self.assertIn("scrollIntoView", SCROLL_JS)
        self.assertIn("scrollTo", SCROLL_JS)


class TagPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_tag_grava_em_tiktok_tag_e_baixa_video_com_ytdlp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            cfg, storage = config(path, "tiktok_tag"), LocalStorage(path)
            state = StateStore(path / "state.duckdb")
            queue = MediaQueue(cfg, storage, state)
            queue.checkpoints = RemoteCheckpoints(cfg, storage, state, None)
            scraper = AsyncMock()
            scraper.last_profile_stats = None
            scraper.extract_posts.return_value = [parse_item(web_item(11, user="bia"))]
            download = MagicMock(return_value=b"VIDEO")
            fetch = AsyncMock(return_value=(b"COVER", "image/jpeg"))
            with (
                patch("zeezazum.platforms.downloader.tiktok_media.download_video", download),
                patch.object(queue.downloader, "_fetch", new=fetch),
            ):
                await _scrape_one(
                    account=SocialAccount("1", "tiktok_tag", "bolsonaro", "https://www.tiktok.com/tag/bolsonaro"),
                    context=None, scraper=scraper, storage=storage, state=state, queue=queue,
                    max_posts=60, since_date=None, max_attempts=1, backoff_seconds=0,
                )
                await queue.close()
            self.assertEqual(storage.read_bytes("tiktok_tag/bolsonaro/media/11_0.mp4"), b"VIDEO")
            self.assertEqual(storage.read_bytes("tiktok_tag/bolsonaro/media/11_1.jpg"), b"COVER")
            self.assertEqual(download.call_args.args[0], "https://www.tiktok.com/@bia/video/11")
            self.assertEqual(state.get("1", "bolsonaro", "tiktok_tag").status, "done")
            self.assertIsNone(state.get("1", "bolsonaro", "tiktok"))
            posts = storage.read_json("tiktok_tag/bolsonaro/posts.json")
            self.assertEqual(posts[0]["owner_username"], "bia")
            state.close()


@unittest.skipUnless(os.environ.get("ZEEZAZUM_LIVE") == "1", "teste ao vivo: ZEEZAZUM_LIVE=1")
class LiveSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Fala com o TikTok de verdade (sem login). Intermitente por natureza."""

    async def test_lista_e_baixa_um_video_de_perfil_publico(self):
        logging.basicConfig(level=logging.INFO)
        scraper = TikTokScraper()
        posts = await scraper.extract_posts(
            None, "tiktok", max_posts=3, since_date=None, stop_at_post_id=None
        )
        self.assertEqual(len(posts), 3)
        self.assertTrue(all(p.timestamp for p in posts))
        data = await asyncio.to_thread(tiktok_media.download_video, posts[0].media[0].url)
        self.assertGreater(len(data), 50_000)
        self.assertEqual(data[4:8], b"ftyp")  # contêiner mp4


if __name__ == "__main__":
    unittest.main()
