import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
import httpx

from db import Database

logger = logging.getLogger("klassno_client")


class KlassnoClient:
    BASE_URL = "https://api.klassno.app"

    def __init__(self, login: str, password: str, db: Database):
        self.login_val = login
        self.password_val = password
        self.db = db

        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        self.current_user_id: Optional[int] = None
        self.student_iri: Optional[str] = None

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "application/ld+json",
            },
            timeout=15.0,
        )

    async def close(self):
        await self._client.aclose()

    async def authenticate(self) -> bool:
        """Авторизация по логину и паролю"""
        logger.info("Авторизация в myclass.team (api.klassno.app)...")
        payload = {"login": self.login_val, "password": self.password_val}
        try:
            resp = await self._client.post(
                "/authentication/login/student",
                json=payload,
                headers={"Content-Type": "application/ld+json"},
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                self.access_token = data.get("accessToken")
                self.student_iri = data.get("ownedBy")  # например /students/105848
                if self.student_iri:
                    match = re.search(r"\d+", self.student_iri)
                    if match:
                        self.current_user_id = int(match.group(0))

                expires_str = data.get("accessTokenExpiresAt")
                if expires_str:
                    try:
                        self.token_expires_at = datetime.fromisoformat(expires_str)
                    except Exception:
                        self.token_expires_at = None

                logger.info(
                    "Успешная авторизация! Токен получен, студент: %s (ID: %s)",
                    self.student_iri,
                    self.current_user_id,
                )
                return True
            else:
                logger.error("Ошибка авторизации: HTTP %d: %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Исключение при авторизации: %s", e)
            return False

    async def _ensure_token(self):
        """Проверяет валидность токена и обновляет его при необходимости"""
        if not self.access_token:
            await self.authenticate()
            return

        if self.token_expires_at:
            now_utc = datetime.now(timezone.utc)
            # Если токен истекает менее чем через 60 секунд — обновляем
            if (self.token_expires_at - now_utc).total_seconds() < 60:
                logger.info("Срок действия токена истекает, повторный логин...")
                await self.authenticate()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Обёртка над запросом с автоматической авторизацией и ретраем при 401"""
        await self._ensure_token()

        headers = kwargs.pop("headers", {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        resp = await self._client.request(method, path, headers=headers, **kwargs)

        # Если 401 Unauthorized — пробуем перелогиниться один раз
        if resp.status_code == 401:
            logger.warning("Получен 401 Unauthorized. Пробуем переавторизоваться...")
            if await self.authenticate():
                headers["Authorization"] = f"Bearer {self.access_token}"
                resp = await self._client.request(method, path, headers=headers, **kwargs)

        return resp

    async def get_threads(self) -> list[dict[str, Any]]:
        """Получить список всех доступных чатов студента"""
        resp = await self.request("GET", "/threads")
        if resp.status_code == 200:
            data = resp.json()
            return data.get("hydra:member", [])
        logger.error("Ошибка при получении списка чатов: %d %s", resp.status_code, resp.text)
        return []

    async def get_thread_name(self, thread_id: int) -> str:
        """Получить название чата (с кэшированием в SQLite)"""
        cached = self.db.get_cached_thread(thread_id)
        if cached:
            return cached

        resp = await self.request("GET", f"/threads/{thread_id}")
        name = f"Чат #{thread_id}"
        if resp.status_code == 200:
            data = resp.json()
            title = data.get("name") or data.get("title")
            if title:
                name = str(title).strip()
                self.db.cache_thread(thread_id, name)
        return name

    async def get_messages(self, thread_id: int, min_id: Optional[int] = None) -> list[dict[str, Any]]:
        """
        Получить сообщения чата:
        - Если min_id задан: запрашивает новые сообщения с id > min_id по возрастанию (order[id]=asc)
        - Если min_id None: запрашивает последнюю страницу по убыванию (для инициализации)
        """
        if min_id is not None:
            url = f"/threads/{thread_id}/messages?order[id]=asc&id[gt]={min_id}"
        else:
            url = f"/threads/{thread_id}/messages?order[id]=desc&page=1"

        resp = await self.request("GET", url)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("hydra:member", [])

        logger.error("Ошибка получения сообщений треда %d: %d %s", thread_id, resp.status_code, resp.text)
        return []

    async def get_user_name(self, user_iri_or_id: str | int) -> str:
        """
        Получить имя пользователя по IRI ('/users/120628') или ID (120628).
        Кэширует результат в SQLite.
        """
        if isinstance(user_iri_or_id, int):
            user_id = user_iri_or_id
        else:
            match = re.search(r"\d+", str(user_iri_or_id))
            if not match:
                return "Неизвестный пользователь"
            user_id = int(match.group(0))

        cached = self.db.get_cached_user(user_id)
        if cached:
            return cached

        # Запрашиваем информацию о пользователе
        resp = await self.request("GET", f"/users/{user_id}")
        if resp.status_code != 200:
            return f"Пользователь #{user_id}"

        user_data = resp.json()
        ep_iri = user_data.get("educationProcessUser")
        name = f"Пользователь #{user_id}"

        if ep_iri:
            # ep_iri может быть '/teachers/120628' или '/students/105848'
            resp_ep = await self.request("GET", ep_iri)
            if resp_ep.status_code == 200:
                ep_data = resp_ep.json()
                first = ep_data.get("firstName") or ""
                last = ep_data.get("lastName") or ""
                full = f"{first} {last}".strip()
                if full:
                    name = full

        self.db.cache_user(user_id, name)
        return name

    async def get_file_info(self, file_iri_or_id: str | int) -> Optional[dict[str, Any]]:
        """Получить информацию о файле вложения (/files/404371)"""
        if isinstance(file_iri_or_id, int):
            path = f"/files/{file_iri_or_id}"
        else:
            path = str(file_iri_or_id)
            if not path.startswith("/"):
                path = f"/{path}"

        resp = await self.request("GET", path)
        if resp.status_code == 200:
            return resp.json()
        return None

