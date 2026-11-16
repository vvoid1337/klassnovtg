import asyncio
import logging
import re
import time
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

        self._auth_fail_count: int = 0
        self._auth_retry_after: float = 0.0

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "application/ld+json",
            },
            timeout=httpx.Timeout(
                connect=5.0,
                read=25.0,
                write=10.0,
                pool=5.0,
            ),
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=30.0,
            ),
        )

    async def close(self):
        await self._client.aclose()

    @staticmethod
    def _safe_json(resp: Optional[httpx.Response]) -> Optional[Any]:
        """Безопасное извлечение JSON из ответа сервера"""
        if resp is None:
            return None
        content = resp.content
        if not content or not content.strip():
            logger.warning("Получен пустой ответ от сервера (HTTP %s, 0 байт)", resp.status_code)
            return None
        try:
            return resp.json()
        except Exception as e:
            logger.warning(
                "Ошибка декодирования JSON (HTTP %s, длина %d байт): %r",
                resp.status_code,
                len(content),
                e,
            )
            return None

    def _record_auth_failure(self, reason: str):
        """Регистрирует неудачную авторизацию и устанавливает экспоненциальный бэкофф"""
        self._auth_fail_count += 1
        # Экспоненциальная задержка: 15с -> 30с -> 60с -> 120с -> максимум 300с (5 минут)
        delay = min(15 * (2 ** (self._auth_fail_count - 1)), 300)
        self._auth_retry_after = time.monotonic() + delay
        logger.warning(
            "Неудачная авторизация #%d (%s). Следующая попытка не ранее чем через %d сек.",
            self._auth_fail_count,
            reason,
            delay,
        )

    async def authenticate(self) -> bool:
        """Авторизация по логину и паролю"""
        now_mono = time.monotonic()
        if now_mono < self._auth_retry_after:
            wait_left = int(self._auth_retry_after - now_mono)
            logger.warning("Авторизация отложена: действует кулдаун ещё %d сек.", wait_left)
            return False

        logger.info("Авторизация в myclass.team (api.klassno.app)...")
        payload = {"login": self.login_val, "password": self.password_val}
        try:
            resp = await self._client.post(
                "/authentication/login/student",
                json=payload,
                headers={"Content-Type": "application/ld+json"},
            )
            if resp.status_code in (200, 201):
                data = self._safe_json(resp)
                if not data or not isinstance(data, dict):
                    self._record_auth_failure("сервер вернул невалидный JSON")
                    return False

                token = data.get("accessToken")
                if not token:
                    self._record_auth_failure("в ответе отсутствует accessToken")
                    return False

                self.access_token = token
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

                # Сброс счетчика ошибок при успешном входе
                self._auth_fail_count = 0
                self._auth_retry_after = 0.0

                logger.info(
                    "Успешная авторизация! Токен получен, студент: %s (ID: %s)",
                    self.student_iri,
                    self.current_user_id,
                )
                return True
            else:
                body_sample = re.sub(r"\s+", " ", resp.text[:200]).strip()
                logger.error("Ошибка авторизации: HTTP %d: %s", resp.status_code, body_sample)
                self._record_auth_failure(f"HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error("Исключение при авторизации: %r", e, exc_info=True)
            self._record_auth_failure(type(e).__name__)
            return False

    async def _ensure_token(self) -> bool:
        """Проверяет валидность токена и обновляет его при необходимости.
        Возвращает True, если токен валиден или успешно обновлен, иначе False."""
        now_utc = datetime.now(timezone.utc)
        now_mono = time.monotonic()

        if self.access_token:
            if not self.token_expires_at:
                return True
            if (self.token_expires_at - now_utc).total_seconds() >= 60:
                return True
            logger.info("Срок действия токена истекает, повторный логин...")

        if now_mono < self._auth_retry_after:
            wait_left = int(self._auth_retry_after - now_mono)
            logger.debug("Пропуск попытки авторизации: действует кулдаун (ещё %d сек.)", wait_left)
            return False

        return await self.authenticate()

    async def request(self, method: str, path: str, retries: int = 2, **kwargs) -> Optional[httpx.Response]:
        """Обёртка над запросом с автоматической авторизацией, ретраями и защитой от таймаутов"""
        has_token = await self._ensure_token()
        if not has_token and not self.access_token:
            return None

        headers = kwargs.pop("headers", {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(method, path, headers=headers, **kwargs)

                # Если 401 Unauthorized — пробуем перелогиниться один раз
                if resp.status_code == 401:
                    logger.warning("Получен 401 Unauthorized. Пробуем переавторизоваться...")
                    self.access_token = None
                    if await self.authenticate():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        resp = await self._client.request(method, path, headers=headers, **kwargs)

                return resp

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < retries:
                    logger.warning("Сетевой сбой при запросе к %s (%s). Повтор через 2 сек...", path, type(e).__name__)
                    await asyncio.sleep(2.0)
                else:
                    logger.error("Не удалось выполнить запрос к %s после %d попыток: %r", path, retries + 1, e)
                    return None
            except Exception as e:
                logger.error("Неожиданная ошибка запроса к %s: %r", path, e, exc_info=True)
                return None

        return None

    async def get_threads(self) -> list[dict[str, Any]]:
        """Получить список всех доступных чатов студента"""
        resp = await self.request("GET", "/threads")
        if resp is not None and resp.status_code == 200:
            data = self._safe_json(resp)
            if isinstance(data, dict):
                return data.get("hydra:member", [])
            return []
        if resp is not None:
            body_sample = re.sub(r"\s+", " ", resp.text[:200]).strip()
            logger.error("Ошибка при получении списка чатов: %d %s", resp.status_code, body_sample)
        return []

    async def get_thread_name(self, thread_id: int) -> str:
        """Получить название чата (с кэшированием в SQLite)"""
        cached = self.db.get_cached_thread(thread_id)
        if cached:
            return cached

        resp = await self.request("GET", f"/threads/{thread_id}")
        name = f"Чат #{thread_id}"
        if resp is not None and resp.status_code == 200:
            data = self._safe_json(resp)
            if isinstance(data, dict):
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
        if resp is not None and resp.status_code == 200:
            data = self._safe_json(resp)
            if isinstance(data, dict):
                return data.get("hydra:member", [])
            return []

        if resp is not None:
            body_sample = re.sub(r"\s+", " ", resp.text[:200]).strip()
            logger.error("Ошибка получения сообщений треда %d: %d %s", thread_id, resp.status_code, body_sample)
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
        if resp is None or resp.status_code != 200:
            return f"Пользователь #{user_id}"

        user_data = self._safe_json(resp)
        name = f"Пользователь #{user_id}"
        if not isinstance(user_data, dict):
            return name

        ep_iri = user_data.get("educationProcessUser")
        if ep_iri:
            # ep_iri может быть '/teachers/120628' или '/students/105848'
            resp_ep = await self.request("GET", ep_iri)
            if resp_ep is not None and resp_ep.status_code == 200:
                ep_data = self._safe_json(resp_ep)
                if isinstance(ep_data, dict):
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
        if resp is not None and resp.status_code == 200:
            data = self._safe_json(resp)
            if isinstance(data, dict):
                return data
        return None

