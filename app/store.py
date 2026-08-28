from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StreamStore:
    """Small persistent registry; runtime process state remains in StreamManager."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS streams (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    video_path TEXT NOT NULL,
                    stream_name TEXT NOT NULL UNIQUE,
                    loop INTEGER NOT NULL,
                    processing_mode TEXT NOT NULL,
                    desired_running INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )

    def create(
        self,
        stream_id: str,
        original_filename: str,
        video_path: Path,
        stream_name: str,
        loop: bool,
        processing_mode: str,
        desired_running: bool = False,
    ) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO streams (
                    id, original_filename, video_path, stream_name, loop,
                    processing_mode, desired_running, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stream_id,
                    original_filename,
                    str(video_path),
                    stream_name,
                    int(loop),
                    processing_mode,
                    int(desired_running),
                    created_at,
                ),
            )
        return self.get(stream_id)

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM streams ORDER BY created_at ASC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, stream_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM streams WHERE id = ?", (stream_id,)
            ).fetchone()
        if not row:
            raise KeyError(stream_id)
        return self._row(row)

    def set_desired_running(self, stream_id: str, running: bool) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE streams SET desired_running = ? WHERE id = ?",
                (int(running), stream_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(stream_id)

    def set_all_desired_running(self, running: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE streams SET desired_running = ?", (int(running),)
            )

    def delete(self, stream_id: str) -> dict[str, Any]:
        record = self.get(stream_id)
        with self._connect() as connection:
            connection.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
        return record

    def delete_all(self) -> list[dict[str, Any]]:
        records = self.list()
        with self._connect() as connection:
            connection.execute("DELETE FROM streams")
        return records

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["loop"] = bool(data["loop"])
        data["desired_running"] = bool(data["desired_running"])
        return data
