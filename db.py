import sqlite3
from pathlib import Path
from typing import Optional


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    thread_id INTEGER NOT NULL,
                    created_at TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS users_cache (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS threads_cache (
                    thread_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_processed_thread ON processed_messages(thread_id);
                """
            )

    # --- Состояние тредов ---
    def get_last_message_id(self, thread_id: int) -> Optional[int]:
        key = f"last_message_id_{thread_id}"
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            return int(row["value"]) if row and row["value"].isdigit() else None

    def set_last_message_id(self, thread_id: int, message_id: int) -> None:
        key = f"last_message_id_{thread_id}"
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(message_id)),
            )

    # --- Обработанные сообщения ---
    def is_message_processed(self, message_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
            return cursor.fetchone() is not None

    def mark_message_processed(self, thread_id: int, message_id: int, created_at: str = "") -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, thread_id, created_at) VALUES (?, ?, ?)",
                (message_id, thread_id, created_at),
            )

    # --- Кэш пользователей ---
    def get_cached_user(self, user_id: int) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT name FROM users_cache WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row["name"] if row else None

    def cache_user(self, user_id: int, name: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO users_cache (user_id, name) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
                (user_id, name),
            )

    # --- Кэш названий чатов ---
    def get_cached_thread(self, thread_id: int) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT name FROM threads_cache WHERE thread_id = ?", (thread_id,))
            row = cursor.fetchone()
            return row["name"] if row else None

    def cache_thread(self, thread_id: int, name: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO threads_cache (thread_id, name) VALUES (?, ?) ON CONFLICT(thread_id) DO UPDATE SET name = excluded.name",
                (thread_id, name),
            )

