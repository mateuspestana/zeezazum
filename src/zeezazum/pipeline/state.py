"""Estado por candidato em DuckDB — permite retomar execuções interrompidas e
raspar incrementalmente em execuções futuras (ex.: cron diário no EC2)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import shutil

from zeezazum.models import CandidateState

# Timestamps são guardados como VARCHAR ISO8601 (sempre UTC), não TIMESTAMP nativo:
# o driver DuckDB converte TIMESTAMP tz-aware para horário local e devolve naive,
# o que quebraria comparações com datetimes UTC-aware no resto do pipeline.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_state (
    candidate_id VARCHAR,
    platform VARCHAR DEFAULT 'instagram',
    handle VARCHAR,
    status VARCHAR,
    newest_post_id VARCHAR,
    newest_post_timestamp VARCHAR,
    total_posts_seen BIGINT,
    last_run_at VARCHAR,
    media_pending BIGINT,
    PRIMARY KEY (candidate_id, platform, handle)
);
"""

_UPSERT_SQL = """
INSERT INTO candidate_state
    (candidate_id, platform, handle, status, newest_post_id, newest_post_timestamp,
     total_posts_seen, last_run_at, media_pending)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (candidate_id, platform, handle) DO UPDATE SET
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
        columns = (
            self._con.execute("PRAGMA table_info('candidate_state')").fetchall()
            if self._con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='candidate_state'"
            ).fetchone()[0]
            else []
        )
        if columns and "platform" not in {c[1] for c in columns}:
            self._con.execute("CHECKPOINT")
            shutil.copy2(self._path, str(self._path) + ".pre-v023.bak")
            self._con.execute("BEGIN")
            try:
                self._con.execute("ALTER TABLE candidate_state RENAME TO legacy_state")
                self._con.execute(_SCHEMA)
                self._con.execute(
                    "INSERT INTO candidate_state SELECT candidate_id, 'instagram', lower(handle), status, newest_post_id, newest_post_timestamp, total_posts_seen, last_run_at, 0 FROM legacy_state"
                )
                self._con.execute("DROP TABLE legacy_state")
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
        self._con.execute(_SCHEMA)
        self._con.execute("""CREATE TABLE IF NOT EXISTS media_tasks (
            platform VARCHAR, handle VARCHAR, shortcode VARCHAR, item_index BIGINT,
            kind VARCHAR, url VARCHAR, status VARCHAR, error VARCHAR,
            PRIMARY KEY(platform, handle, shortcode, item_index))""")

    def get(
        self, candidate_id: str, handle: str, platform: str = "instagram"
    ) -> CandidateState | None:
        row = self._con.execute(
            "SELECT candidate_id, handle, status, newest_post_id, newest_post_timestamp, "
            "total_posts_seen, last_run_at, media_pending FROM candidate_state "
            "WHERE candidate_id = ? AND handle = ? AND platform = ?",
            [candidate_id, handle.lower(), platform],
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
                "instagram",
                state.handle.lower(),
                state.status,
                state.newest_post_id,
                _to_iso(state.newest_post_timestamp),
                state.total_posts_seen,
                _to_iso(state.last_run_at),
                state.media_pending,
            ],
        )

    def register_media(self, handle, shortcode, item, *, exists=False):
        self._con.execute(
            """INSERT INTO media_tasks VALUES ('instagram', ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(platform, handle, shortcode, item_index) DO UPDATE SET
            url=excluded.url, kind=excluded.kind, status=excluded.status, error=NULL""",
            [
                handle.lower(),
                shortcode,
                item.index,
                item.kind.value,
                item.url,
                "done" if exists else "pending",
            ],
        )

    def finish_media(self, handle, shortcode, index, error=None):
        self._con.execute(
            "UPDATE media_tasks SET status=?, error=? WHERE platform='instagram' AND handle=? AND shortcode=? AND item_index=?",
            ["failed" if error else "done", error, handle.lower(), shortcode, index],
        )

    def pending_media(self, handle, images=True, videos=True):
        kinds = [k for k, enabled in [("image", images), ("video", videos)] if enabled]
        return self._con.execute(
            "SELECT shortcode, item_index, kind, url FROM media_tasks WHERE platform='instagram' AND handle=? AND status!='done' AND kind IN (SELECT unnest(?))",
            [handle.lower(), kinds],
        ).fetchall()

    def refresh_pending(self, images=True, videos=True):
        for (handle,) in self._con.execute(
            "SELECT DISTINCT handle FROM candidate_state"
        ).fetchall():
            self._con.execute(
                "UPDATE candidate_state SET media_pending=? WHERE handle=? AND platform='instagram'",
                [len(self.pending_media(handle, images, videos)), handle],
            )

    def snapshot(self):
        self._con.execute("CHECKPOINT")
        return self._path.read_bytes()

    def close(self) -> None:
        self._con.close()
