"""CLI do Zeezazum."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import polars as pl
import typer

from zeezazum.config import load_config
from zeezazum.input.loader import read_table, write_table
from zeezazum.input.url_parser import extract_instagram_accounts
from zeezazum.models import SocialAccount
from zeezazum.pipeline.orchestrator import run_scrape

app = typer.Typer(help="Zeezazum — raspador universalizável de redes sociais (Playwright)")


def _accounts_from_table(df: pl.DataFrame, *, id_column: str, url_column: str) -> list[SocialAccount]:
    accounts: list[SocialAccount] = []
    for row_id, text in zip(df[id_column].cast(pl.Utf8), df[url_column]):
        accounts.extend(extract_instagram_accounts(str(row_id), text))
    return accounts


@app.command()
def prepare(
    input: Path = typer.Option(..., "--input", help="Tabela de origem (parquet/csv/xlsx)"),
    column: str = typer.Option(..., "--column", help="Coluna de texto livre com as redes sociais"),
    id_column: str = typer.Option(..., "--id-column", help="Coluna identificadora única de cada linha"),
    platform: str = typer.Option("instagram", "--platform", help="Plataforma a extrair (só 'instagram' por ora)"),
    extra_columns: str = typer.Option(
        "",
        "--extra-columns",
        help="Colunas adicionais da tabela de origem a manter no output, separadas por vírgula "
        "(ex.: 'NM_URNA_CANDIDATO,SG_UF,SG_PARTIDO,DS_CARGO')",
    ),
    output: Path = typer.Option(..., "--output", help="Onde salvar a tabela normalizada"),
) -> None:
    """Extrai, de uma coluna de texto livre, só as contas de uma plataforma (ex.: Instagram),
    deduplicadas por linha — gera uma tabela pequena e limpa para alimentar `scrape`.
    Opcionalmente carrega junto outras colunas de identificação da tabela de origem
    (nome, UF, partido etc.), úteis pra conferir/filtrar antes de raspar."""
    if platform != "instagram":
        raise typer.BadParameter("Por enquanto só 'instagram' é suportado.")

    df = read_table(input)
    extra_cols = [c.strip() for c in extra_columns.split(",") if c.strip()]
    missing = [c for c in (column, id_column, *extra_cols) if c not in df.columns]
    if missing:
        raise typer.BadParameter(f"Coluna(s) não encontrada(s) em {input}: {missing}")

    accounts = _accounts_from_table(df, id_column=id_column, url_column=column)
    rows = [{"row_id": a.row_id, "handle": a.handle, "instagram_url": a.url} for a in accounts]
    schema = {"row_id": pl.Utf8, "handle": pl.Utf8, "instagram_url": pl.Utf8}
    result = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)

    if extra_cols:
        lookup = df.select([id_column, *extra_cols]).with_columns(pl.col(id_column).cast(pl.Utf8))
        result = result.join(lookup, left_on="row_id", right_on=id_column, how="left")
        result = result.select(["row_id", *extra_cols, "handle", "instagram_url"])

    write_table(result, output)
    typer.echo(f"{result.height} contas de instagram extraídas de {df.height} linhas -> {output}")


def _load_config_and_accounts(config: Path):
    cfg = load_config(config)
    Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=cfg.logging.level,
        filename=cfg.logging.file,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    df = read_table(cfg.input.path)
    missing = [c for c in (cfg.input.url_column, cfg.input.id_column) if c not in df.columns]
    if missing:
        raise typer.BadParameter(f"Coluna(s) não encontrada(s) em {cfg.input.path}: {missing}")

    accounts = _accounts_from_table(df, id_column=cfg.input.id_column, url_column=cfg.input.url_column)
    return cfg, accounts


@app.command()
def scrape(
    config: Path = typer.Option(Path("config.yaml"), "--config", help="Caminho do config.yaml"),
) -> None:
    """Roda a raspagem (posts + mídia) a partir do config.yaml. Seguro para rodar
    repetidamente (ex.: via cron): candidatos novos são raspados do zero, candidatos
    já conhecidos são atualizados incrementalmente (só posts novos — para assim que
    reencontra o post mais recente já conhecido)."""
    cfg, accounts = _load_config_and_accounts(config)
    if not accounts:
        typer.echo("Nenhuma conta de Instagram encontrada no input. Nada a fazer.")
        raise typer.Exit(code=0)

    typer.echo(f"Raspando {len(accounts)} perfis do Instagram...")
    asyncio.run(run_scrape(cfg, accounts))
    typer.echo("Concluído.")


@app.command()
def backfill(
    config: Path = typer.Option(Path("config.yaml"), "--config", help="Caminho do config.yaml"),
    extra_posts: int = typer.Option(
        ..., "--extra-posts", help="Quantos posts a mais buscar, além do que já foi salvo, por conta."
    ),
) -> None:
    """Aprofunda o histórico de contas já raspadas antes, buscando posts mais antigos
    que ainda não foram salvos — ao contrário de `scrape`, NÃO para no post mais
    recente já conhecido. Para uma conta com N posts já salvos, busca até N + EXTRA_POSTS
    no total (os N já salvos são deduplicados, então só os realmente novos são baixados).
    Contas ainda não raspadas usam o `max_posts` normal do config.yaml."""
    if extra_posts <= 0:
        raise typer.BadParameter("--extra-posts precisa ser um número positivo.")

    cfg, accounts = _load_config_and_accounts(config)
    if not accounts:
        typer.echo("Nenhuma conta de Instagram encontrada no input. Nada a fazer.")
        raise typer.Exit(code=0)

    typer.echo(f"Aprofundando histórico de {len(accounts)} perfis do Instagram (+{extra_posts} posts cada)...")
    asyncio.run(run_scrape(cfg, accounts, extra_posts=extra_posts))
    typer.echo("Concluído.")


if __name__ == "__main__":
    app()
