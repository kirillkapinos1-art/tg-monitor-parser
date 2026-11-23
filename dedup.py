"""SQLite deduplication store for processed Telegram posts and matched leads."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path


class SeenPostsStore:
    """Tracks processed (chat_id, message_id) pairs and content hashes to avoid duplicates."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        os.makedirs(Path(self.db_path).resolve().parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_posts (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    text_hash TEXT NOT NULL,
                    forwarded INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_hash ON seen_posts (text_hash)"
            )

    def is_seen(self, chat_id: int, message_id: int, text_hash: str) -> bool:
        """Return True if either the exact message or identical text hash was already processed."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM seen_posts
                WHERE (chat_id = ? AND message_id = ?) OR text_hash = ?
                LIMIT 1
                """,
                (int(chat_id), int(message_id), text_hash),
            ).fetchone()
        return row is not None

    def mark_seen(
        self,
        *,
        chat_id: int,
        message_id: int,
        text_hash: str,
        forwarded: bool,
        score: int = 0,
    ) -> None:
        """Record a processed message and whether it was forwarded as a lead."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO seen_posts (chat_id, message_id, text_hash, forwarded, score)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    text_hash = excluded.text_hash,
                    forwarded = excluded.forwarded,
                    score = excluded.score
                """,
                (
                    int(chat_id),
                    int(message_id),
                    text_hash,
                    1 if forwarded else 0,
                    int(score),
                ),
            )

    def count_forwarded(self) -> int:
        """Return total number of forwarded leads recorded."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM seen_posts WHERE forwarded = 1"
            ).fetchone()
        return int(row[0]) if row else 0
