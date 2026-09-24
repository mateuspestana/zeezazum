"""Estado por candidato em DuckDB — permite retomar execuções interrompidas e
raspar incrementalmente em execuções futuras (ex.: cron diário no EC2)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

from zeezazum.models import CandidateState

# Timestamps são guardados como VARCHAR ISO8601 (sempre UTC), não TIMESTAMP nativo:
# o driver DuckDB converte TIMESTAMP tz-aware para horário local e devolve naive,
# o que quebraria comparações com datetimes UTC-aware no resto do pipeline.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_state (
    candidate_id VARCHAR PRIMARY KEY,
    handle VARCHAR,
    status VARCHAR,
    newest_post_id VARCHAR,
    newest_post_timestamp VARCHAR,
    total_posts_seen BIGINT,
    last_run_at VARCHAR,
    media_pending BIGINT
);
"""

_UPSERT_SQL = """
INSERT INTO candidate_state
    (candidate_id, handle, status, newest_post_id, newest_post_timestamp,
     total_posts_seen, last_run_at, media_pending)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (candidate_id) DO UPDATE SET
    handle = excluded.handle,
    status = excluded.status,
    newest_post_id = excluded.newest_post_id,
    newest_post_timestamp = excluded.newest_post_timestamp,
    total_posts_seen = excluded.total_posts_seen,
    last_run_at = excluded.last_run_at,
    media_pending = excluded.media_pending
"""


def _to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StateStore:
    """Uma única conexão DuckDB reaproveitada durante toda a execução — o DuckDB
    é embutido e não tolera múltiplos processos escrevendo no mesmo arquivo ao
    mesmo tempo, então não abrimos/fechamos conexão por chamada."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self._path))
        self._con.execute(_SCHEMA)

    def get(self, candidate_id: str) -> CandidateState | None:
        row = self._con.execute(
            "SELECT candidate_id, handle, status, newest_post_id, newest_post_timestamp, "
            "total_posts_seen, last_run_at, media_pending FROM candidate_state "
            "WHERE candidate_id = ?",
            [candidate_id],
        ).fetchone()
        if row is None:
            return None
        return CandidateState(
            candidate_id=row[0],
            handle=row[1],
            status=row[2],
            newest_post_id=row[3],
            newest_post_timestamp=_from_iso(row[4]),
            total_posts_seen=row[5] or 0,
            last_run_at=_from_iso(row[6]),
            media_pending=row[7] or 0,
        )

    def upsert(self, state: CandidateState) -> None:
        self._con.execute(
            _UPSERT_SQL,
            [
                state.candidate_id,
                state.handle,
                state.status,
                state.newest_post_id,
                _to_iso(state.newest_post_timestamp),
                state.total_posts_seen,
                _to_iso(state.last_run_at),
                state.media_pending,
            ],
        )

    def close(self) -> None:
        self._con.close()
