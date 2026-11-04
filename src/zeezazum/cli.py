"""CLI do Zeezazum."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote, quote_plus

import polars as pl
import typer

from zeezazum.config import load_config
from zeezazum.input.loader import read_table, write_table
from zeezazum.input.url_parser import extract_accounts
from zeezazum.models import SocialAccount
from zeezazum.platforms import DISPLAY_NAMES, PLATFORMS
from zeezazum.pipeline.orchestrator import run_media_sync, run_scrape


def _execute(coroutine):
    from zeezazum.platforms.base import AuthenticationRequired
    from zeezazum.pipeline.orchestrator import PersistenceFailure

    try:
        return asyncio.run(coroutine)
    except AuthenticationRequired as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except PersistenceFailure as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


app = typer.Typer(
    help="Zeezazum — raspador universalizável de redes sociais (Playwright)"
)


def _accounts_from_table(
    df: pl.DataFrame, *, id_column: str, url_column: str, platform: str = "instagram"
) -> list[SocialAccount]:
    accounts: list[SocialAccount] = []
    for row_id, text in zip(df[id_column].cast(pl.Utf8), df[url_column]):
        accounts.extend(extract_accounts(platform, str(row_id), text))
    return accounts


@app.command()
def prepare(
    input: Path = typer.Option(
        ..., "--input", help="Tabela de origem (parquet/csv/xlsx)"
    ),
    column: str = typer.Option(
        ..., "--column", help="Coluna de texto livre com as redes sociais"
    ),
    id_column: str = typer.Option(
        ..., "--id-column", help="Coluna identificadora única de cada linha"
    ),
    platform: str = typer.Option(
        "instagram", "--platform", help="Plataforma a extrair: instagram, tiktok, tiktok_tag ou tiktok_search"
    ),
    extra_columns: str = typer.Option(
        "",
        "--extra-columns",
        help="Colunas adicionais da tabela de origem a manter no output, separadas por vírgula "
        "(ex.: 'NM_URNA_CANDIDATO,SG_UF,SG_PARTIDO,DS_CARGO')",
    ),
    output: Path = typer.Option(
        ..., "--output", help="Onde salvar a tabela normalizada"
    ),
) -> None:
    """Extrai, de uma coluna de texto livre, só as contas, tags ou buscas de uma plataforma (Instagram ou TikTok),
    deduplicadas por linha — gera uma tabela pequena e limpa para alimentar `scrape`.
    Opcionalmente carrega junto outras colunas de identificação da tabela de origem
    (nome, UF, partido etc.), úteis pra conferir/filtrar antes de raspar."""
    if platform not in PLATFORMS:
        raise typer.BadParameter(f"Plataforma não suportada. Use uma de: {', '.join(PLATFORMS)}.")

    df = read_table(input)
    extra_cols = [c.strip() for c in extra_columns.split(",") if c.strip()]
    missing = [c for c in (column, id_column, *extra_cols) if c not in df.columns]
    if missing:
        raise typer.BadParameter(f"Coluna(s) não encontrada(s) em {input}: {missing}")

    accounts = _accounts_from_table(
        df, id_column=id_column, url_column=column, platform=platform
    )
    url_col = f"{platform}_url"
    rows = [
        {"row_id": a.row_id, "handle": a.handle, url_col: a.url} for a in accounts
    ]
    schema = {"row_id": pl.Utf8, "handle": pl.Utf8, url_col: pl.Utf8}
    result = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)

    if extra_cols:
        lookup = df.select([id_column, *extra_cols]).with_columns(
            pl.col(id_column).cast(pl.Utf8)
        )
        result = result.join(lookup, left_on="row_id", right_on=id_column, how="left")
        result = result.select(["row_id", *extra_cols, "handle", url_col])

    write_table(result, output)
    typer.echo(
        f"{result.height} contas de {platform} extraídas de {df.height} linhas -> {output}"
    )


def _load_config_and_accounts(config: Path):
    cfg = load_config(config)
    Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=cfg.logging.level,
        filename=cfg.logging.file,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(
        logging.WARNING
    )  # uma linha por arquivo baixado polui o log

    df = read_table(cfg.input.path)
    missing = [
        c for c in (cfg.input.url_column, cfg.input.id_column) if c not in df.columns
    ]
    if missing:
        raise typer.BadParameter(
            f"Coluna(s) não encontrada(s) em {cfg.input.path}: {missing}"
        )

    accounts = _accounts_from_table(
        df,
        id_column=cfg.input.id_column,
        url_column=cfg.input.url_column,
        platform=cfg.platform,
    )
    return cfg, accounts


@app.command()
def scrape(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", help="Caminho do config.yaml"
    ),
) -> None:
    """Roda a raspagem (posts + mídia) a partir do config.yaml. Seguro para rodar
    repetidamente (ex.: via cron): candidatos novos são raspados do zero, candidatos
    já conhecidos são atualizados incrementalmente (só posts novos — para assim que
    reencontra o post mais recente já conhecido)."""
    cfg, accounts = _load_config_and_accounts(config)
    if not accounts:
        typer.echo(
            f"Nenhuma conta de {DISPLAY_NAMES[cfg.platform]} encontrada no input. Nada a fazer."
        )
        raise typer.Exit(code=0)

    typer.echo(f"Raspando {len(accounts)} perfis do {DISPLAY_NAMES[cfg.platform]}...")
    _execute(run_scrape(cfg, accounts))
    typer.echo("Concluído.")


@app.command()
def backfill(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", help="Caminho do config.yaml"
    ),
    extra_posts: int | None = typer.Option(
        None,
        "--extra-posts",
        help="Quantos posts a mais buscar, além do que já foi salvo, por conta.",
    ),
    all_posts: bool = typer.Option(
        False,
        "--all",
        help="Baixa o histórico completo: ignora max_posts e since_date e só para "
        "quando a plataforma deixa de entregar posts.",
    ),
) -> None:
    """Aprofunda o histórico de contas já raspadas antes, buscando posts mais antigos
    que ainda não foram salvos — ao contrário de `scrape`, NÃO para no post mais
    recente já conhecido. Para uma conta com N posts já salvos, busca até N + EXTRA_POSTS
    no total (os N já salvos são deduplicados, então só os realmente novos são baixados).
    Contas ainda não raspadas usam o `max_posts` normal do config.yaml."""
    if all_posts == (extra_posts is not None):
        raise typer.BadParameter("Use --extra-posts N ou --all (um dos dois).")
    if extra_posts is not None and extra_posts <= 0:
        raise typer.BadParameter("--extra-posts precisa ser um número positivo.")

    cfg, accounts = _load_config_and_accounts(config)
    if not accounts:
        typer.echo(
            f"Nenhuma conta de {DISPLAY_NAMES[cfg.platform]} encontrada no input. Nada a fazer."
        )
        raise typer.Exit(code=0)

    alvo = "histórico completo" if all_posts else f"+{extra_posts} posts cada"
    typer.echo(
        f"Aprofundando histórico de {len(accounts)} perfis do {DISPLAY_NAMES[cfg.platform]} ({alvo})..."
    )
    _execute(run_scrape(cfg, accounts, extra_posts=extra_posts, fetch_all=all_posts))
    typer.echo("Concluído.")


@app.command("tiktok-session")
def tiktok_session(
    config: Path = typer.Option(
        Path("config.yaml"),
        "--config",
        help="config.yaml, de onde sai o caminho da sessão (scraping.tiktok_session_file)",
    ),
    output: Path | None = typer.Option(
        None, "--output", help="Onde salvar a sessão (padrão: o do config.yaml)"
    ),
    browser: str = typer.Option("chromium", "--browser", help="chromium ou firefox"),
    tag: str | None = typer.Option(
        None, "--tag", help="Abre a página dessa hashtag (sem #) em vez de um perfil"
    ),
    search: str | None = typer.Option(
        None, "--search", help="Abre a busca de vídeos por esse texto em vez de um perfil"
    ),
) -> None:
    """Resolve o captcha do TikTok à mão, uma vez. Abre uma janela visível, você desliza
    o quebra-cabeça (sem fechar o modal pelo X), e a sessão só é salva quando os vídeos aparecem para o `scrape` reaproveitar.
    Rode numa máquina com interface gráfica e copie o arquivo para o servidor, se preciso."""
    from zeezazum.platforms.tiktok.session import DEFAULT_SESSION_FILE, capture_session

    if output is None:
        output = Path(
            load_config(config).scraping.tiktok_session_file
            if config.exists()
            else DEFAULT_SESSION_FILE
        )
    urls = []
    if tag:
        urls.append(f"https://www.tiktok.com/tag/{quote(tag.lstrip('#'))}")
    if search:
        urls.append(f"https://www.tiktok.com/search/video?q={quote_plus(search)}")
    kwargs = {"urls": urls} if urls else {}
    ok = capture_session(output, browser_name=browser, echo=typer.echo, **kwargs)
    if not ok:
        typer.echo(
            "Sessão não gravada: o captcha não foi resolvido (vídeos não apareceram). "
            "Se já existia uma sessão, ela foi mantida. Rode o comando de novo.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Sessão salva em {output}")
    typer.echo("Pronto: rode o `scrape` de novo.")


@app.command("media-sync")
def media_sync(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", help="Caminho do config.yaml"
    ),
) -> None:
    """Baixa a mídia que falta em disco para posts já raspados (recupera downloads
    interrompidos). Não abre navegador e não raspa nada novo."""
    cfg, accounts = _load_config_and_accounts(config)
    result = _execute(run_media_sync(cfg, accounts))
    for handle, n in result.items():
        typer.echo(f"@{handle}: {n} arquivos reenfileirados")
    typer.echo("Concluído.")


if __name__ == "__main__":
    app()
