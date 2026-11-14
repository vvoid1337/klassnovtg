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

                CREATE TABLE IF NOT EXISTS whitelist (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS tg_users_cache (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS access_requests (
                    user_id INTEGER PRIMARY KEY,
                    status TEXT DEFAULT 'pending', -- pending, rejected, approved, blocked
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    # --- Белый список (Whitelist) ---
    def is_user_whitelisted(self, user_id: int) -> bool:
        """Проверяет, находится ли пользователь в белом списке"""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None

    def add_to_whitelist(
        self, user_id: int, username: str = "", first_name: str = "", added_by: int = 0
    ) -> None:
        """Добавляет пользователя в белый список или обновляет информацию о нем"""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO whitelist (user_id, username, first_name, added_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    added_by = excluded.added_by
                """,
                (user_id, username, first_name, added_by),
            )

    def remove_from_whitelist(self, user_id: int) -> bool:
        """Удаляет пользователя из белого списка. Возвращает True, если был удален"""
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    def get_whitelist(self) -> list[dict]:
        """Возвращает всех пользователей из белого списка"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT user_id, username, first_name, added_by, added_at FROM whitelist ORDER BY added_at ASC"
            )
            return [dict(row) for row in cursor.fetchall()]

    # --- Кэш Telegram профилей (для авто-подстановки имен) ---
    def save_tg_user(self, user_id: int, username: str = "", first_name: str = "") -> None:
        """Сохраняет или обновляет профиль Telegram пользователя"""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tg_users_cache (user_id, username, first_name, last_seen)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE tg_users_cache.username END,
                    first_name = CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE tg_users_cache.first_name END,
                    last_seen = CURRENT_TIMESTAMP
                """,
                (user_id, username, first_name),
            )

    def get_tg_user(self, user_id: int) -> Optional[dict]:
        """Возвращает профиль пользователя из tg_users_cache"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT user_id, username, first_name FROM tg_users_cache WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # --- Запросы на доступ (анти-спам в личку админа) ---
    def get_access_request_status(self, user_id: int) -> Optional[str]:
        """Возвращает статус заявки: 'pending', 'rejected', 'approved', 'blocked' или None"""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT status FROM access_requests WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row["status"] if row else None

    def record_access_request(self, user_id: int, status: str = "pending") -> None:
        """Фиксирует факт отправки запроса или меняет его статус"""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO access_requests (user_id, status, requested_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = excluded.status,
                    requested_at = CURRENT_TIMESTAMP
                """,
                (user_id, status),
            )

    def delete_access_request(self, user_id: int) -> None:
        """Удаляет запись о запросе"""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM access_requests WHERE user_id = ?", (user_id,))




