"""Leitura genérica de tabelas de input (parquet/csv/xlsx) via polars."""
from __future__ import annotations

from pathlib import Path

import polars as pl


def read_table(path: str | Path) -> pl.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pl.read_parquet(path)
    if suffix == ".csv":
        return pl.read_csv(path)
    if suffix in (".xlsx", ".xls"):
        return pl.read_excel(path)
    raise ValueError(f"Formato de input não suportado: {suffix} ({path})")


def write_table(df: pl.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df.write_parquet(path)
    elif suffix == ".csv":
        df.write_csv(path)
    else:
        raise ValueError(f"Formato de output não suportado: {suffix} ({path})")
