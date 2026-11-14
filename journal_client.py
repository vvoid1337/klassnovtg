import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional
import httpx

logger = logging.getLogger("journal_client")
MSK_TZ = timezone(timedelta(hours=3))


def format_teacher_name(full_name: str) -> str:
    """Сокращает ФИО: 'Баранов Константин Сергеевич' -> 'Баранов К.С.'"""
    if not full_name:
        return ""
    parts = full_name.strip().split()
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1][0]}.{parts[2][0]}."
    elif len(parts) == 2:
        return f"{parts[0]} {parts[1][0]}."
    return full_name


def format_room(room_name: str) -> str:
    """Форматирует название кабинета: 'Аудитория 10' -> 'каб. 10'"""
    if not room_name:
        return ""
    r = room_name.strip()
    r = re.sub(r"^(аудитория|ауд\.?)\s*", "каб. ", r, flags=re.IGNORECASE)
    if "спортивный" in r.lower():
        return "Спортзал"
    return r


class JournalClient:
    BASE_URL = "https://msapi.top-academy.ru"
    APP_KEY = "6a56a5df2667e65aab73ce76d1dd737f7d1faef9c52e8b8c55ac75f565d8e8a6"
    CACHE_TTL_SECONDS = 600  # Кэшируем расписание на 10 минут

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Origin": "https://journal.top-academy.ru",
                "Referer": "https://journal.top-academy.ru/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    async def authenticate(self) -> bool:
        """Авторизация в msapi.top-academy.ru"""
        logger.info("Авторизация в журнале (msapi.top-academy.ru)...")
        payload = {
            "application_key": self.APP_KEY,
            "id_city": None,
            "username": self.username,
            "password": self.password,
        }

        try:
            resp = await self._client.post("/api/v2/auth/login", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                self.access_token = data.get("access_token")
                exp_ts = data.get("expires_in_access")
                if exp_ts:
                    self.token_expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
                logger.info("Успешная авторизация в журнале Top Academy!")
                return True
            else:
                logger.error("Ошибка авторизации в журнале: %d %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Исключение при авторизации в журнале: %s", e)
            return False

    async def _ensure_token(self):
        if not self.access_token:
            await self.authenticate()
            return

        if self.token_expires_at:
            now_utc = datetime.now(timezone.utc)
            if (self.token_expires_at - now_utc).total_seconds() < 60:
                await self.authenticate()

    async def get_month_schedule(self, date_filter: str) -> list[dict[str, Any]]:
        """Получить расписание на месяц (с кэшированием в памяти на 10 минут)"""
        month_key = date_filter[:7]
        now = time.time()
        if month_key in self._cache:
            cached_time, cached_data = self._cache[month_key]
            if now - cached_time < self.CACHE_TTL_SECONDS:
                return cached_data

        await self._ensure_token()
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        for attempt in range(3):
            try:
                resp = await self._client.get(
                    f"/api/v2/schedule/operations/get-month?date_filter={date_filter}",
                    headers=headers,
                )
                if resp.status_code == 401:
                    logger.warning("Токен журнала истек (401), перелогиниваемся...")
                    if await self.authenticate():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        resp = await self._client.get(
                            f"/api/v2/schedule/operations/get-month?date_filter={date_filter}",
                            headers=headers,
                        )
                if resp.status_code == 200:
                    data = resp.json()
                    res_list = data if isinstance(data, list) else []
                    self._cache[month_key] = (now, res_list)
                    return res_list
                logger.error("Ошибка получения расписания: %d %s", resp.status_code, resp.text)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning("Сетевая задержка при получении расписания (%s), повтор...", type(e).__name__)
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.error("Неожиданная ошибка получения расписания: %s", e)
                break

        return []

    async def get_schedule_for_date(self, target_date: date) -> list[dict[str, Any]]:
        """Получить список пар на конкретную дату"""
        date_str = target_date.strftime("%Y-%m-%d")
        month_data = await self.get_month_schedule(date_str)
        day_lessons = [item for item in month_data if item.get("date") == date_str]
        day_lessons.sort(key=lambda x: x.get("lesson", 0))
        return day_lessons

    @staticmethod
    def format_schedule_card(lessons: list[dict[str, Any]], target_date: date) -> str:
        """Стильная чистая карточка расписания с blockquote и monospace"""
        weekdays_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        months_ru = [
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря"
        ]

        weekday_name = weekdays_ru[target_date.weekday()]
        month_name = months_ru[target_date.month - 1]

        header = f"📅 <b>{weekday_name.upper()}, {target_date.day} {month_name.upper()}</b>"

        if not lessons:
            return (
                f"{header}\n\n"
                "<blockquote>ℹ️ <b>Пары ещё не выставлены</b></blockquote>"
            )

        lines = []
        for item in lessons:
            num = item.get("lesson", 1)
            start = item.get("started_at", "")
            end = item.get("finished_at", "")
            time_str = f"{start} - {end}" if start and end else ""
            subj = html.escape(item.get("subject_name") or "Пара")
            room = html.escape(format_room(item.get("room_name") or ""))
            teacher = html.escape(format_teacher_name(item.get("teacher_name") or ""))

            line = f"<b>{num}.</b> <code>{time_str}</code> · <b>{subj}</b>"

            details = []
            if room:
                details.append(f"<code>{room}</code>")
            if teacher:
                details.append(teacher)
            if details:
                line += f"\n   └ {' · '.join(details)}"

            lines.append(line)

        content = "\n\n".join(lines)

        first_time = lessons[0].get("started_at", "")
        last_time = lessons[-1].get("finished_at", "")
        time_span = f" (с {first_time} до {last_time})" if first_time and last_time else ""
        summary = f"\n\n📊 <i>Всего пар: {len(lessons)}{time_span}</i>"

        return f"{header}\n\n<blockquote>{content}</blockquote>{summary}"

    @staticmethod
    def format_week_overview(start_week: date) -> str:
        """Карточка-заглушка при смене недели с предложением выбрать день"""
        end_week = start_week + timedelta(days=5)
        start_str = start_week.strftime("%d.%m")
        end_str = end_week.strftime("%d.%m")

        months_ru = [
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря"
        ]
        month_name = months_ru[start_week.month - 1]

        header = f"🗓️ <b>УЧЕБНАЯ НЕДЕЛЯ ({start_str} – {end_str})</b>"
        body = "<blockquote>👇 <b>Выберите день недели на кнопках ниже</b></blockquote>"
        return f"{header}\n\n{body}"

    @staticmethod
    def build_schedule_inline_keyboard(start_week: date, selected_date: Optional[date] = None) -> dict[str, Any]:
        """Создает инлайн-кнопки дней недели. Если selected_date is None, ни один день не выделен"""
        weekdays_short = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

        # Ряд 1: Все 6 дней недели строго в одну строку (Пн - Сб)
        row_days = []
        for i in range(6):
            d = start_week + timedelta(days=i)
            is_selected = (selected_date is not None and d == selected_date)
            label = f"•{weekdays_short[i]} {d.day}•" if is_selected else f"{weekdays_short[i]} {d.day}"
            row_days.append({"text": label, "callback_data": f"sched:{d.strftime('%Y-%m-%d')}"})

        prev_week = start_week - timedelta(days=7)
        next_week = start_week + timedelta(days=7)
        today_dt = datetime.now(MSK_TZ).date()

        # Ряд 2: Навигация между неделями (передаем понедельник целевой недели) и возврат на Сегодня
        row_nav = [
            {"text": "◀️ Неделя", "callback_data": f"week:{prev_week.strftime('%Y-%m-%d')}"},
            {"text": "🎯 Сегодня", "callback_data": f"sched:{today_dt.strftime('%Y-%m-%d')}"},
            {"text": "Неделя ▶️", "callback_data": f"week:{next_week.strftime('%Y-%m-%d')}"},
        ]

        return {"inline_keyboard": [row_days, row_nav]}
