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
from typer.testing import CliRunner
from yt_dlp.utils import DownloadError, ExtractorError

from zeezazum.cli import app
from zeezazum.config import ZeezazumConfig
from zeezazum.input.url_parser import extract_accounts, extract_tiktok_accounts
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
from zeezazum.platforms.base import ProfileUnavailable
from zeezazum.platforms.instagram.scraper import InstagramScraper
from zeezazum.platforms.tiktok import media as tiktok_media
from zeezazum.platforms.tiktok.parser import parse_entry, parse_user_detail
from zeezazum.platforms.tiktok.scraper import TikTokScraper
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
