import logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Any, Optional

import logging
import re
import time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Any, Optional

from config import Config
from db import Database
from journal_client import JournalClient
from tg_sender import TelegramSender

logger = logging.getLogger("telegram_bot")

MSK_TZ = timezone(timedelta(hours=3))
STICKER_PATH = Path(__file__).resolve().parent / "var" / "sticker" / "litvin_hi.webp"

# Единственная кнопка в постоянной клавиатуре внизу экрана
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📅 Показать расписание"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


class TelegramBotHandler:
    def __init__(self, tg: TelegramSender, journal: JournalClient, db: Database):
        self.tg = tg
        self.journal = journal
        self.db = db
        self.last_update_id: Optional[int] = None
        self.admin_ids = Config.get_admin_ids()
        self._user_last_msg_time: dict[int, float] = {}
        # Запоминаем ID последнего отправленного расписания для каждого чата: {chat_id: message_id}
        self._last_schedule_msg_id: dict[int | str, int] = {}

    def _is_allowed(self, user_id: int) -> bool:
        """Проверяет доступ пользователя: админ или пользователь в белом списке"""
        if user_id in self.admin_ids:
            return True
        return self.db.is_user_whitelisted(user_id)

    def _check_rate_limit(self, user_id: int, cooldown_seconds: float = 3.0) -> bool:
        """Возвращает True, если лимит превышен (флуд), иначе обновляет время и возвращает False"""
        now = time.time()
        last_time = self._user_last_msg_time.get(user_id, 0.0)
        if now - last_time < cooldown_seconds:
            return True
        self._user_last_msg_time[user_id] = now
        return False

    async def poll_and_handle_updates(self):
        """Опрашивает Telegram на наличие новых сообщений и кликов по кнопкам"""
        updates = await self.tg.get_updates(offset=self.last_update_id, timeout=5)
        if not updates:
            return

        for update in updates:
            self.last_update_id = update["update_id"] + 1
            try:
                # 1. Проверяем клик по Inline-кнопкам дней недели
                callback = update.get("callback_query")
                if callback:
                    await self._process_callback(callback)
                    continue

                # 2. Обычные текстовые сообщения / нажатие кнопки "Показать расписание"
                message = update.get("message")
                if not message:
                    continue

                chat = message.get("chat", {})
                chat_id = chat.get("id")
                chat_type = chat.get("type", "")
                from_user = message.get("from", {})
                user_id = from_user.get("id")

                if not chat_id or not user_id:
                    continue

                # Запоминаем профиль пользователя (username, first_name)
                user_name = from_user.get("first_name", "")
                username = from_user.get("username", "")
                self.db.save_tg_user(user_id=user_id, username=username, first_name=user_name)

                # Игнорируем сообщения в беседах/супергруппах, чтобы не создавать флуд
                if chat_type != "private":
                    continue

                text = (message.get("text") or "").strip()

                # Проверяем админские команды управления вайтлистом
                if user_id in self.admin_ids and (
                    text.startswith("/wl")
                    or text.startswith("/whitelist")
                    or message.get("forward_from")
                    or message.get("forward_origin")
                ):
                    handled = await self._process_admin_command(chat_id, user_id, text, message)
                    if handled:
                        continue

                # Проверка прав доступа для просмотра расписания
                if not self._is_allowed(user_id):
                    # Проверяем антифлуд (не чаще 1 ответа в 3 сек)
                    if self._check_rate_limit(user_id, cooldown_seconds=3.0):
                        continue

                    # Проверяем статус запроса: если пользователь заблокирован, молчим
                    req_status = self.db.get_access_request_status(user_id)
                    if req_status == "blocked":
                        continue

                    # Отправляем сообщение пользователю
                    await self.tg.send_text(
                        f"⛔ <b>Доступ ограничен</b>\n\n"
                        f"Привет, {user_name or 'пользователь'}!\n"
                        f"Вы не добавлены в вайтлист.\n"
                        f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
                        f"Для добавления в вайтлист напишите: @vvoid",
                        target_chat_id=chat_id,
                    )

                    # Уведомляем администраторов о новом запросе ТОЛЬКО ОДИН РАЗ
                    # Если запрос уже был отправлен ('pending' или 'rejected'), не беспокоим админа снова
                    if req_status is None:
                        self.db.record_access_request(user_id, status="pending")

                        admin_btn = {
                            "inline_keyboard": [
                                [
                                    {
                                        "text": f"✅ Добавить {user_name or user_id}",
                                        "callback_data": f"wl_accept:{user_id}",
                                    },
                                    {
                                        "text": "🚫 Отклонить",
                                        "callback_data": f"wl_reject:{user_id}",
                                    },
                                ],
                                [
                                    {
                                        "text": "⛔ Заблокировать",
                                        "callback_data": f"wl_block:{user_id}",
                                    }
                                ]
                            ]
                        }
                        u_link = f"@{username}" if username else "нет"
                        for admin_chat in self.admin_ids:
                            await self.tg.send_text(
                                f"🔔 <b>Запрос доступа к расписанию</b>\n\n"
                                f"👤 <b>Имя:</b> {user_name or 'Без имени'}\n"
                                f"🔗 <b>Username:</b> {u_link}\n"
                                f"🆔 <b>ID:</b> <code>{user_id}</code>",
                                target_chat_id=admin_chat,
                                reply_markup=admin_btn,
                            )
                    continue

                # Реагируем ТОЛЬКО на команду /start и кнопку "📅 Показать расписание"
                norm_text = text.lower()
                is_start_cmd = norm_text.startswith("/start")
                is_schedule_btn = "показать расписание" in norm_text

                if not (is_start_cmd or is_schedule_btn):
                    # Любые другие обычные сообщения в личке просто игнорируем
                    continue

                await self._process_command(chat_id, text)
            except Exception as e:
                logger.error("Ошибка при обработке update %s: %s", update.get("update_id"), e, exc_info=True)

    async def _process_admin_command(
        self, chat_id: int | str, admin_id: int, text: str, message: dict[str, Any]
    ) -> bool:
        """Обрабатывает команды управления белым списком от администратора"""
        # 1. Проверяем пересланное сообщение для быстрого добавления
        forward_from = message.get("forward_from")
        # В новых апдейтах Telegram может быть forward_origin
        forward_origin = message.get("forward_origin", {})
        target_fwd_user = forward_from or forward_origin.get("sender_user")

        if target_fwd_user and (not text or text.lower().startswith("/wl") or text.lower().startswith("/add")):
            target_id = target_fwd_user.get("id")
            first_name = target_fwd_user.get("first_name", "")
            username = target_fwd_user.get("username", "")

            self.db.add_to_whitelist(
                user_id=target_id,
                username=username,
                first_name=first_name,
                added_by=admin_id,
            )
            name_display = f"@{username}" if username else first_name or str(target_id)
            await self.tg.send_text(
                f"✅ Пользователь <b>{name_display}</b> (ID: <code>{target_id}</code>) успешно добавлен в вайтлист!",
                target_chat_id=chat_id,
            )
            return True

        if not (text.startswith("/wl") or text.startswith("/whitelist")):
            return False

        parts = text.split()
        cmd = parts[1].lower() if len(parts) > 1 else "list"

        if cmd in ("list", "ls", "all"):
            whitelist = self.db.get_whitelist()
            if not whitelist:
                msg = (
                    "📋 <b>Вайтлист пуст.</b>\n\n"
                    "Чтобы добавить пользователя:\n"
                    "• <code>/wl add &lt;user_id&gt; [имя]</code>\n"
                    "• Или просто перешлите мне сообщение от нужного человека!"
                )
            else:
                lines = [f"📋 <b>Вайтлист ({len(whitelist)} чел.):</b>\n"]
                for idx, row in enumerate(whitelist, 1):
                    uid = row["user_id"]
                    uname = f"@{row['username']}" if row.get("username") else ""
                    fname = row.get("first_name") or ""
                    display = f"{fname} {uname}".strip() or "Без имени"
                    lines.append(f"{idx}. <b>{display}</b> — <code>{uid}</code>")
                lines.append(
                    "\n<i>Удалить: <code>/wl del &lt;ID&gt;</code>\n"
                    "Добавить: <code>/wl add &lt;ID&gt;</code> или перешлите сообщение.</i>"
                )
                msg = "\n".join(lines)

            await self.tg.send_text(msg, target_chat_id=chat_id)
            return True

        elif cmd == "add":
            if len(parts) < 3:
                await self.tg.send_text(
                    "Использование: <code>/wl add &lt;user_id&gt; [комментарий/имя]</code>\n"
                    "Либо просто перешлите мне сообщение пользователя.",
                    target_chat_id=chat_id,
                )
                return True

            uid_str = parts[2]
            if not uid_str.isdigit():
                await self.tg.send_text("⚠️ Ошибка: user_id должен состоять только из цифр.", target_chat_id=chat_id)
                return True

            target_id = int(uid_str)
            note = " ".join(parts[3:]) if len(parts) > 3 else ""

            # Если комментарий не указан, проверяем кэш профилей пользователей
            cached_u = self.db.get_tg_user(target_id) or {}
            username = cached_u.get("username", "")
            first_name = note or cached_u.get("first_name", "")

            self.db.add_to_whitelist(
                user_id=target_id,
                username=username,
                first_name=first_name,
                added_by=admin_id,
            )

            name_display = f"@{username}" if username else first_name or "без имени"
            await self.tg.send_text(
                f"✅ Пользователь <b>{name_display}</b> (ID: <code>{target_id}</code>) добавлен в вайтлист!",
                target_chat_id=chat_id,
            )
            return True

        elif cmd in ("del", "remove", "rm"):
            if len(parts) < 3:
                await self.tg.send_text(
                    "Использование: <code>/wl del &lt;user_id&gt;</code>",
                    target_chat_id=chat_id,
                )
                return True

            uid_str = parts[2]
            if not uid_str.isdigit():
                await self.tg.send_text("⚠️ Ошибка: user_id должен состоять только из цифр.", target_chat_id=chat_id)
                return True

            target_id = int(uid_str)
            removed = self.db.remove_from_whitelist(target_id)
            self.db.delete_access_request(target_id)
            if removed:
                await self.tg.send_text(f"🗑 ID <code>{target_id}</code> удален из вайтлиста.", target_chat_id=chat_id)
            else:
                await self.tg.send_text(f"⚠️ ID <code>{target_id}</code> не найден в вайтлисте.", target_chat_id=chat_id)
            return True

        else:
            await self.tg.send_text(
                "ℹ️ <b>Команды управления вайтлистом:</b>\n"
                "• <code>/wl</code> — список всех пользователей\n"
                "• <code>/wl add &lt;id&gt; [имя]</code> — добавить по ID\n"
                "• <code>/wl del &lt;id&gt;</code> — удалить по ID\n"
                "• Пересылка сообщения от человека боту — быстрое добавление",
                target_chat_id=chat_id,
            )
            return True

    async def _process_callback(self, cb: dict[str, Any]):
        """Обработка кликов по интерактивным inline-кнопкам дней недели"""
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        from_user = cb.get("from", {})
        user_id = from_user.get("id")

        if not cb_id or not chat_id or not message_id or not user_id:
            return

        # Гасим индикатор загрузки на кнопке
        await self.tg.answer_callback_query(cb_id)

        # 0. Одобрение добавления в вайтлист админом в 1 клик
        if data.startswith("wl_accept:"):
            if user_id not in self.admin_ids:
                return

            target_uid_str = data.split("wl_accept:", 1)[1]
            if not target_uid_str.isdigit():
                return
            target_uid = int(target_uid_str)

            cached_u = self.db.get_tg_user(target_uid) or {}
            target_username = cached_u.get("username", "")
            target_fname = cached_u.get("first_name", "")

            self.db.add_to_whitelist(
                user_id=target_uid,
                username=target_username,
                first_name=target_fname,
                added_by=user_id,
            )
            self.db.record_access_request(target_uid, status="approved")

            name_display = f"@{target_username}" if target_username else target_fname or str(target_uid)

            # Обновляем админское сообщение (убираем кнопку и пишем, что добавлен)
            await self.tg.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"✅ <b>Доступ предоставлен!</b>\n\n"
                    f"👤 <b>Пользователь:</b> {name_display}\n"
                    f"🆔 <b>ID:</b> <code>{target_uid}</code>\n"
                    f"Добавлен в вайтлист администратором."
                ),
            )

            # Уведомляем самого пользователя, что доступ открыт
            try:
                await self.tg.send_text(
                    "🎉 <b>Вам открыт доступ к боту!</b>\n\n"
                    "Нажмите кнопку ниже или введите /start, чтобы открыть расписание.",
                    target_chat_id=target_uid,
                    reply_markup=MAIN_KEYBOARD,
                )
            except Exception as e:
                logger.warning("Не удалось отправить уведомление пользователю %s: %s", target_uid, e)
            return

        # 0.1 Отклонение запроса админом
        if data.startswith("wl_reject:"):
            if user_id not in self.admin_ids:
                return

            target_uid_str = data.split("wl_reject:", 1)[1]
            if not target_uid_str.isdigit():
                return
            target_uid = int(target_uid_str)

            # Удаляем запись о запросе, чтобы при следующем обращении юзер снова мог отправить запрос
            self.db.delete_access_request(target_uid)
            cached_u = self.db.get_tg_user(target_uid) or {}
            target_username = cached_u.get("username", "")
            target_fname = cached_u.get("first_name", "")
            name_display = f"@{target_username}" if target_username else target_fname or str(target_uid)

            await self.tg.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"❌ <b>Запрос отклонён.</b>\n\n"
                    f"👤 <b>Пользователь:</b> {name_display}\n"
                    f"🆔 <b>ID:</b> <code>{target_uid}</code>\n"
                    f"<i>Запрос сброшен. Если он напишет снова, вам придет новое уведомление.</i>"
                ),
            )
            return

        # 0.2 Блокировка спамера админом
        if data.startswith("wl_block:"):
            if user_id not in self.admin_ids:
                return

            target_uid_str = data.split("wl_block:", 1)[1]
            if not target_uid_str.isdigit():
                return
            target_uid = int(target_uid_str)

            self.db.record_access_request(target_uid, status="blocked")
            self.db.remove_from_whitelist(target_uid)
            cached_u = self.db.get_tg_user(target_uid) or {}
            target_username = cached_u.get("username", "")
            target_fname = cached_u.get("first_name", "")
            name_display = f"@{target_username}" if target_username else target_fname or str(target_uid)

            await self.tg.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"⛔ <b>Пользователь заблокирован!</b>\n\n"
                    f"👤 <b>Пользователь:</b> {name_display}\n"
                    f"🆔 <b>ID:</b> <code>{target_uid}</code>\n"
                    f"<i>Бот будет полностью игнорировать любые его сообщения.</i>"
                ),
            )
            return

        # Проверка доступа к кнопкам расписания
        if not self._is_allowed(user_id):
            if self._check_rate_limit(user_id, cooldown_seconds=3.0):
                return
            if self.db.get_access_request_status(user_id) == "blocked":
                return
            user_name = from_user.get("first_name", "пользователь")
            await self.tg.send_text(
                f"⛔ <b>Доступ ограничен</b>\n\n"
                f"Привет, {user_name}!\n"
                f"Вы не добавлены в вайтлист.\n"
                f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
                f"Для добавления в вайтлист напишите: @vvoid",
                target_chat_id=chat_id,
            )
            return

        if data.startswith("week:"):
            date_str = data.split("week:", 1)[1]
            try:
                start_week = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                return

            overview_text = self.journal.format_week_overview(start_week)
            inline_kb = self.journal.build_schedule_inline_keyboard(start_week, selected_date=None)

            await self.tg.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=overview_text,
                reply_markup=inline_kb,
            )
            return

        if data.startswith("sched:"):
            date_str = data.split("sched:", 1)[1]
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                return

            lessons = await self.journal.get_schedule_for_date(target_date)
            card_text = self.journal.format_schedule_card(lessons, target_date)
            start_week = target_date - timedelta(days=target_date.weekday())
            inline_kb = self.journal.build_schedule_inline_keyboard(start_week, selected_date=target_date)

            # Обновляем сообщение прямо на месте
            await self.tg.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=card_text,
                reply_markup=inline_kb,
            )

    async def _process_command(self, chat_id: int | str, raw_text: str):
        now_msk = datetime.now(MSK_TZ).date()

        # Если сегодня воскресенье — сразу открываем ближайший понедельник
        if now_msk.weekday() == 6:
            target_date = now_msk + timedelta(days=1)
        else:
            target_date = now_msk

        # При команде /start отправляем стикер и выставляем постоянную кнопку внизу
        if raw_text.strip().lower().startswith("/start"):
            if STICKER_PATH.exists():
                await self.tg.send_sticker(
                    target_chat_id=chat_id,
                    sticker_path=str(STICKER_PATH),
                    reply_markup=MAIN_KEYBOARD,
                )
            else:
                await self.tg.send_text(
                    "👋 <b>Бот готов!</b> Нажимай кнопку ниже в любой момент.",
                    target_chat_id=chat_id,
                    reply_markup=MAIN_KEYBOARD,
                )

        # Удаляем предыдущее сообщение с расписанием, если оно было
        prev_msg_id = self._last_schedule_msg_id.get(chat_id)
        if prev_msg_id:
            await self.tg.delete_message(chat_id=chat_id, message_id=prev_msg_id)

        # Открываем интерактивное расписание с инлайн-кнопками по дням
        lessons = await self.journal.get_schedule_for_date(target_date)
        card = self.journal.format_schedule_card(lessons, target_date)
        start_week = target_date - timedelta(days=target_date.weekday())
        inline_kb = self.journal.build_schedule_inline_keyboard(start_week, selected_date=target_date)

        new_msg_id = await self.tg.send_text(
            card,
            target_chat_id=chat_id,
            reply_markup=inline_kb,
        )
        if new_msg_id:
            self._last_schedule_msg_id[chat_id] = new_msg_id
