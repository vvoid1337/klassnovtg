import html
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
import httpx

logger = logging.getLogger("tg_sender")

# Часовой пояс МСК (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))


def clean_body_text(raw_text: str) -> str:
    """Очищает текст от артефактов API Platform / Markdown экранирования"""
    if not raw_text:
        return ""

    # Декодируем html сущности вроде &#x20;, &quot;, &amp;
    text = html.unescape(raw_text)

    # Убираем лишние обратные слэши перед спецсимволами (\=, \_, \-, \., \+)
    text = re.sub(r"\\([=_*\-\[\]()~`>#+!|{}])", r"\1", text)

    # Приводим множественные пустые строки к аккуратному виду
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_timestamp(iso_str: str) -> str:
    """Преобразует ISO UTC время в строку МСК (ДД.ММ ЧЧ:ММ)"""
    try:
        dt = datetime.fromisoformat(iso_str)
        dt_msk = dt.astimezone(MSK_TZ)
        return dt_msk.strftime("%d.%m %H:%M")
    except Exception:
        return ""


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self):
        await self._client.aclose()

    async def check_bot(self) -> bool:
        """Проверяет токен бота (getMe)"""
        if not self.bot_token or self.bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
            logger.error("TELEGRAM_BOT_TOKEN не задан в .env!")
            return False

        try:
            resp = await self._client.get(f"{self.api_url}/getMe")
            if resp.status_code == 200 and resp.json().get("ok"):
                bot_user = resp.json()["result"].get("username")
                logger.info("Telegram бот успешно подключен: @%s", bot_user)
                return True
            else:
                logger.error("Неверный токен Telegram бота: %s", resp.text)
                return False
        except Exception as e:
            logger.error("Ошибка соединения с Telegram API: %s", e)
            return False

    async def send_text(self, text: str, parse_mode: Optional[str] = "HTML") -> bool:
        """Отправляет текстовое сообщение в беседу (с разбивкой на части при > 4000 символов)"""
        if not self.chat_id or self.chat_id == "YOUR_TELEGRAM_CHAT_ID":
            logger.error("TELEGRAM_CHAT_ID не задан в .env!")
            return False

        chunks = self._chunk_text(text, max_len=4000)
        success = True

        for chunk in chunks:
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            try:
                resp = await self._client.post(f"{self.api_url}/sendMessage", json=payload)
                if resp.status_code != 200:
                    # Если ошибка парсинга HTML, пробуем отправить чистым текстом
                    if parse_mode and "can't parse entities" in resp.text:
                        logger.warning("Ошибка парсинга HTML, отправляем чистым текстом...")
                        payload.pop("parse_mode", None)
                        payload["text"] = re.sub(r"<[^>]+>", "", chunk)
                        resp = await self._client.post(f"{self.api_url}/sendMessage", json=payload)

                    if resp.status_code != 200:
                        logger.error("Ошибка отправки в Telegram: %d %s", resp.status_code, resp.text)
                        success = False
            except Exception as e:
                logger.error("Исключение при отправке сообщения в TG: %s", e)
                success = False

        return success

    async def send_photo(self, photo_url: str, caption: str = "") -> bool:
        """Отправляет фото по URL"""
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
        }
        if caption:
            # Для caption лимит 1024 символа
            if len(caption) > 1000:
                caption = caption[:997] + "..."
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"

        try:
            resp = await self._client.post(f"{self.api_url}/sendPhoto", json=payload)
            if resp.status_code == 200:
                return True
            else:
                logger.warning("Не удалось отправить фото по URL: %s. Пробуем скачать и отправить файлом...", resp.text)
                # Скачиваем фото и шлём multipart
                download_resp = await self._client.get(photo_url)
                if download_resp.status_code == 200:
                    files = {"photo": ("image.jpg", download_resp.content, "image/jpeg")}
                    data = {"chat_id": self.chat_id}
                    if caption:
                        data["caption"] = caption
                        data["parse_mode"] = "HTML"
                    resp2 = await self._client.post(f"{self.api_url}/sendPhoto", data=data, files=files)
                    return resp2.status_code == 200
        except Exception as e:
            logger.error("Исключение при отправке фото в TG: %s", e)
        return False

    async def send_document(self, doc_url: str, filename: str = "document", caption: str = "") -> bool:
        """Отправляет документ/файл"""
        payload = {
            "chat_id": self.chat_id,
            "document": doc_url,
        }
        if caption:
            payload["caption"] = caption[:1000]
            payload["parse_mode"] = "HTML"

        try:
            resp = await self._client.post(f"{self.api_url}/sendDocument", json=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.error("Исключение при отправке документа в TG: %s", e)
            return False

    async def forward_klassno_message(
        self,
        thread_name: str,
        author_name: str,
        created_at_iso: str,
        raw_body: str,
        attachments_info: list[dict[str, Any]],
    ) -> bool:
        """Форматирует сообщение из Классно и пересылает в Telegram"""
        time_str = format_timestamp(created_at_iso)
        time_part = f" <i>({time_str})</i>" if time_str else ""

        header = f"📌 <b>{html.escape(thread_name)}</b>\n💬 <b>{html.escape(author_name)}</b>{time_part}\n\n"
        cleaned_body = clean_body_text(raw_body)
        body_escaped = html.escape(cleaned_body)

        # Превращаем простые ссылки [title](url) в <a href="url">title</a>
        body_formatted = re.sub(
            r"\[([^\]]+)\]\((https?://[^\)]+)\)",
            r'<a href="\2">\1</a>',
            body_escaped,
        )

        full_text = header + (body_formatted if body_formatted else "<i>[Без текста]</i>")

        # Если есть фото-вложение и оно единственное
        photos = [att for att in attachments_info if att.get("type") == "photo" and att.get("url")]
        other_files = [att for att in attachments_info if att.get("type") != "photo" and att.get("url")]

        if photos and len(photos) == 1 and not other_files and len(full_text) <= 1000:
            # Отправляем фото с подписью
            return await self.send_photo(photos[0]["url"], caption=full_text)

        # Иначе отправляем сначала текст сообщения
        success = await self.send_text(full_text, parse_mode="HTML")

        # Затем отправляем все фото и файлы
        for photo in photos:
            await self.send_photo(photo["url"], caption=f"📎 Фото: {photo.get('originalName', 'image.jpg')}")

        for doc in other_files:
            doc_name = doc.get("originalName") or "file"
            caption = f"📎 Файл: <b>{html.escape(doc_name)}</b>"
            await self.send_document(doc["url"], filename=doc_name, caption=caption)

        return success

    @staticmethod
    def _chunk_text(text: str, max_len: int = 4000) -> list[str]:
        """Разбивает длинный текст по границам строк/абзацев"""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Ищем ближайший перенос строки в пределах лимита
            split_idx = text.rfind("\n", 0, max_len)
            if split_idx == -1 or split_idx < max_len // 2:
                # Если перенос далеко, разбиваем по пробелу
                split_idx = text.rfind(" ", 0, max_len)
            if split_idx == -1:
                split_idx = max_len

            chunks.append(text[:split_idx])
            text = text[split_idx:].lstrip()

        return chunks

