"""SQLite: Apple UID ↔ Google event ID 매핑 및 변경 추적."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


@dataclass
class LinkRecord:
    apple_uid: str
    google_event_id: str
    google_ical_uid: str
    apple_fp: str
    google_fp: str
    last_origin: str  # apple | google
    updated_at: str


class SyncStore:
    def __init__(self, db_path: str):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                apple_uid TEXT PRIMARY KEY,
                google_event_id TEXT NOT NULL,
                google_ical_uid TEXT NOT NULL,
                apple_fp TEXT NOT NULL,
                google_fp TEXT NOT NULL,
                last_origin TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_links_google ON links(google_event_id);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_by_apple(self, apple_uid: str) -> Optional[LinkRecord]:
        row = self._conn.execute(
            "SELECT * FROM links WHERE apple_uid = ?", (apple_uid,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_google(self, google_event_id: str) -> Optional[LinkRecord]:
        row = self._conn.execute(
            "SELECT * FROM links WHERE google_event_id = ?", (google_event_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def all_links(self) -> Dict[str, LinkRecord]:
        rows = self._conn.execute("SELECT * FROM links").fetchall()
        return {r["apple_uid"]: self._row_to_record(r) for r in rows}

    def upsert(
        self,
        apple_uid: str,
        google_event_id: str,
        google_ical_uid: str,
        apple_fp: str,
        google_fp: str,
        last_origin: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO links(
                apple_uid, google_event_id, google_ical_uid,
                apple_fp, google_fp, last_origin, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apple_uid) DO UPDATE SET
                google_event_id = excluded.google_event_id,
                google_ical_uid = excluded.google_ical_uid,
                apple_fp = excluded.apple_fp,
                google_fp = excluded.google_fp,
                last_origin = excluded.last_origin,
                updated_at = excluded.updated_at
            """,
            (
                apple_uid,
                google_event_id,
                google_ical_uid,
                apple_fp,
                google_fp,
                last_origin,
                now,
            ),
        )
        self._conn.commit()

    def delete(self, apple_uid: str) -> None:
        self._conn.execute("DELETE FROM links WHERE apple_uid = ?", (apple_uid,))
        self._conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> LinkRecord:
        return LinkRecord(
            apple_uid=row["apple_uid"],
            google_event_id=row["google_event_id"],
            google_ical_uid=row["google_ical_uid"],
            apple_fp=row["apple_fp"],
            google_fp=row["google_fp"],
            last_origin=row["last_origin"],
            updated_at=row["updated_at"],
        )
