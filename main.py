import asyncio
import logging
import random
import signal
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

from config import Config
from db import Database
from klassno_client import KlassnoClient
from journal_client import JournalClient
from tg_sender import TelegramSender
from telegram_bot import TelegramBotHandler

# Настройка цветного логирования
class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",     # Голубой
        "INFO": "\033[32m",      # Зеленый
        "WARNING": "\033[33m",   # Желтый
        "ERROR": "\033[31m",     # Красный
        "CRITICAL": "\033[35m",   # Пурпурный
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        orig_levelname = record.levelname
        color = self.COLORS.get(orig_levelname, "")
        record.levelname = f"{color}{orig_levelname:<5}{self.RESET}"
        formatted = super().format(record)
        record.levelname = orig_levelname
        return formatted


log_level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setFormatter(
    ColoredFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
)

logging.basicConfig(level=log_level, handlers=[log_handler])

# Заглушаем болтливые сторонние библиотеки
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger("main")

MSK_TZ = timezone(timedelta(hours=3))


def calculate_sleep_interval(base_interval: int) -> float:
    """
    Рассчитывает интервал с рандомизацией (джиттером) и ночным режимом:
    - Ночью (01:00 - 07:00 МСК): опрос раз в 2-4 минуты
    - Днём: базовый интервал + случайные секунды (-3..+7 сек)
    """
    now_msk = datetime.now(MSK_TZ)
    # Ночной режим: с 1 до 7 утра по Москве
    if 1 <= now_msk.hour < 7:
        # Ночью опрашиваем редко (120 - 180 секунд)
        return random.uniform(120, 180)

    # Днём добавляем случайный разброс, чтобы не выглядеть как робот
    jitter = random.uniform(-2.0, 6.0)
    delay = max(8.0, base_interval + jitter)
    return delay


async def main():
    logger.info("=== Запуск Klassno -> Telegram Forwarder ===")

    # Флаг для корректной остановки
    stop_event = asyncio.Event()

    def handle_exit():
        logger.info("Получен сигнал завершения работы...")
        stop_event.set()

    # Для Windows и Linux
    try:
        loop = asyncio.get_running_loop()

        # Глушим известный баг Windows asyncio ProactorEventLoop (WinError 10054 при разрыве соединений)
        def _asyncio_exception_handler(current_loop, context):
            exc = context.get("exception")
            if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
                return
            current_loop.default_exception_handler(context)

        loop.set_exception_handler(_asyncio_exception_handler)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_exit)
            except NotImplementedError:
                pass
    except Exception:
        pass

    # 1. Валидация настроек
    if not Config.KLASSNO_LOGIN or not Config.KLASSNO_PASSWORD:
        logger.error("ОШИБКА: Заполните KLASSNO_LOGIN и KLASSNO_PASSWORD в файле .env!")
        return

    has_tg_creds = (
        Config.TELEGRAM_BOT_TOKEN
        and Config.TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN"
        and Config.TELEGRAM_CHAT_ID
        and Config.TELEGRAM_CHAT_ID != "YOUR_TELEGRAM_CHAT_ID"
    )

    if not has_tg_creds:
        logger.warning(
            "ВНИМАНИЕ: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не настроены в файле .env!\n"
            "Бот запустится в тестовом режиме (логирует в консоль без отправки в TG)."
        )

    # 2. Инициализация базы данных и клиентов
    db = Database(Config.DB_PATH)
    klassno = KlassnoClient(Config.KLASSNO_LOGIN, Config.KLASSNO_PASSWORD, db)
    journal = JournalClient(Config.KLASSNO_LOGIN, Config.KLASSNO_PASSWORD)
    tg = TelegramSender(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
    bot_handler = TelegramBotHandler(tg, journal, db)

    if has_tg_creds:
        tg_ok = await tg.check_bot()
        if not tg_ok:
            logger.warning("Не удалось проверить Telegram бота. Проверьте токен.")

    # Фоновая задача: слушаем входящие команды Telegram в личке (расписание)
    async def telegram_updates_worker():
        while not stop_event.is_set():
            try:
                if has_tg_creds:
                    await bot_handler.poll_and_handle_updates()
            except Exception as e:
                logger.error("Ошибка при обработке команд Telegram: %s", e)
            await asyncio.sleep(0.2)

    tg_task = asyncio.create_task(telegram_updates_worker())

    # 3. Авторизация в Klassno
    auth_ok = await klassno.authenticate()
    if not auth_ok:
        logger.error("Не удалось авторизоваться в api.klassno.app! Проверьте логин и пароль в .env.")
        await klassno.close()
        await tg.close()
        return

    # 4. Определение отслеживаемых чатов
    configured_threads = Config.get_thread_ids()
    if configured_threads == "all":
        all_threads = await klassno.get_threads()
        target_thread_ids = [t["id"] for t in all_threads if "id" in t]
        logger.info("Режим 'all': отслеживается %d чатов", len(target_thread_ids))
    else:
        target_thread_ids = configured_threads
        logger.info("Отслеживаются чаты с ID: %s", target_thread_ids)

    # 5. Инициализация состояния (чтобы не спамить старыми сообщениями при первом старте)
    for th_id in target_thread_ids:
        th_name = await klassno.get_thread_name(th_id)
        last_id = db.get_last_message_id(th_id)

        if last_id is None:
            # Первый запуск для этого чата
            initial_msgs = await klassno.get_messages(th_id)
            if initial_msgs:
                max_id = max(m["id"] for m in initial_msgs)
                db.set_last_message_id(th_id, max_id)
                for m in initial_msgs:
                    db.mark_message_processed(th_id, m["id"], m.get("createdAt", ""))
                logger.info(
                    "Инициализация чата '%s' (ID %d): зафиксирован последний message_id=%d. "
                    "Старые сообщения не будут отправлены в Telegram.",
                    th_name,
                    th_id,
                    max_id,
                )
            else:
                db.set_last_message_id(th_id, 0)
                logger.info("Инициализация чата '%s' (ID %d): сообщений пока нет.", th_name, th_id)
        else:
            logger.info("Чат '%s' (ID %d) готов. Последний обработанный ID: %d", th_name, th_id, last_id)

    logger.info("Цикл опроса запущен. Интервал: %d сек. Нажмите Ctrl+C для выхода.", Config.POLL_INTERVAL_SECONDS)

    try:
        while not stop_event.is_set():
            for th_id in target_thread_ids:
                try:
                    await check_thread_updates(th_id, klassno, tg, db, has_tg_creds)
                except Exception as e:
                    logger.error("Ошибка при проверке чата %d: %s", th_id, e, exc_info=True)

            # Вычисляем интервал со случайным джиттером и ночным режимом
            sleep_delay = calculate_sleep_interval(Config.POLL_INTERVAL_SECONDS)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_delay)
            except asyncio.TimeoutError:
                pass

    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка по требованию пользователя...")
    finally:
        tg_task.cancel()
        try:
            await tg_task
        except asyncio.CancelledError:
            pass
        await klassno.close()
        await journal.close()
        await tg.close()
        # Даем event loop закрыть оставшиеся сетевые транспорты httpx
        await asyncio.sleep(0.05)
        logger.info("Бот корректно остановлен.")


async def check_thread_updates(
    thread_id: int,
    klassno: KlassnoClient,
    tg: TelegramSender,
    db: Database,
    has_tg_creds: bool,
):
    """Проверяет и отправляет новые сообщения одного чата"""
    last_id = db.get_last_message_id(thread_id)
    if last_id is None:
        return

    # Запрашиваем сообщения с id > last_id по возрастанию
    new_messages = await klassno.get_messages(thread_id, min_id=last_id)
    if not new_messages:
        return

    thread_name = await klassno.get_thread_name(thread_id)

    for msg in new_messages:
        msg_id = msg.get("id")
        if not msg_id or db.is_message_processed(msg_id):
            continue

        created_by_iri = msg.get("createdBy") or ""
        created_at = msg.get("createdAt") or ""
        body = msg.get("body") or ""
        raw_attachments = msg.get("attachments") or []

        # Проверка: сообщение от самого себя
        if not Config.FORWARD_OWN_MESSAGES and klassno.current_user_id:
            if f"/users/{klassno.current_user_id}" in created_by_iri:
                logger.info("Пропущено собственное сообщение ID %d", msg_id)
                db.mark_message_processed(thread_id, msg_id, created_at)
                db.set_last_message_id(thread_id, msg_id)
                continue

        # Получаем имя автора
        author_name = await klassno.get_user_name(created_by_iri)

        # Собираем инфо о вложениях (если есть)
        attachments_info: list[dict[str, Any]] = []
        for att_iri in raw_attachments:
            f_info = await klassno.get_file_info(att_iri)
            if f_info:
                attachments_info.append(f_info)

        logger.info(
            "НОВОЕ СООБЩЕНИЕ [%s] от %s (ID %d): %s (вложений: %d)",
            thread_name,
            author_name,
            msg_id,
            (body[:50] + "...") if len(body) > 50 else body,
            len(attachments_info),
        )

        # Отправляем в Telegram
        if has_tg_creds:
            sent_ok = await tg.forward_klassno_message(
                thread_name=thread_name,
                author_name=author_name,
                created_at_iso=created_at,
                raw_body=body,
                attachments_info=attachments_info,
            )
            if sent_ok:
                logger.info("Успешно доставлено в Telegram: ID %d", msg_id)
            else:
                logger.error("Не удалось доставить в Telegram сообщение ID %d", msg_id)

        # Фиксируем в базе
        db.mark_message_processed(thread_id, msg_id, created_at)
        db.set_last_message_id(thread_id, msg_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

