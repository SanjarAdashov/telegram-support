"""Safe FAQ-first Telegram support bot with human escalation."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from dotenv import load_dotenv
from faq_store import DEFAULT_FAQ, FAQStore
from support_store import SupportStore
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)

FAQ = DEFAULT_FAQ
ADMIN_MINI_APP_URL = os.getenv("ADMIN_MINI_APP_URL", "").strip()


def normalize(text: str) -> str:
    return " ".join(text.casefold().strip().rstrip("?!.").split())


@dataclass(frozen=True)
class Ticket:
    id: int
    user_id: int
    user_label: str
    status: str


class TicketStore:
    def __init__(self, path: str):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                author_id INTEGER NOT NULL,
                author_role TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_user
              ON tickets(user_id) WHERE status = 'open';
            """
        )
        self.connection.commit()

    def active(self, user_id: int) -> Ticket | None:
        row = self.connection.execute(
            "SELECT id, user_id, user_label, status FROM tickets WHERE user_id=? AND status='open'",
            (user_id,),
        ).fetchone()
        return Ticket(**dict(row)) if row else None

    def create_or_get(self, user_id: int, user_label: str, body: str) -> Ticket:
        ticket = self.active(user_id)
        if ticket is None:
            now = datetime.now(timezone.utc).isoformat()
            cursor = self.connection.execute(
                "INSERT INTO tickets(user_id,user_label,created_at) VALUES (?,?,?)",
                (user_id, user_label, now),
            )
            ticket = Ticket(cursor.lastrowid, user_id, user_label, "open")
        self.add_message(ticket.id, user_id, "user", body)
        self.connection.commit()
        return ticket

    def add_message(self, ticket_id: int, author_id: int, role: str, body: str) -> None:
        self.connection.execute(
            "INSERT INTO messages(ticket_id,author_id,author_role,body,created_at) VALUES (?,?,?,?,?)",
            (ticket_id, author_id, role, body, datetime.now(timezone.utc).isoformat()),
        )

    def get(self, ticket_id: int) -> Ticket | None:
        row = self.connection.execute(
            "SELECT id, user_id, user_label, status FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        return Ticket(**dict(row)) if row else None

    def close(self, ticket_id: int) -> bool:
        result = self.connection.execute(
            "UPDATE tickets SET status='closed', closed_at=? WHERE id=? AND status='open'",
            (datetime.now(timezone.utc).isoformat(), ticket_id),
        )
        self.connection.commit()
        return result.rowcount == 1

    def close_connection(self) -> None:
        self.connection.close()


def find_faq_answer(question: str, faq: dict[str, str] = FAQ) -> str | None:
    """Return an approved answer only for an exact normalized FAQ question."""
    return faq.get(normalize(question))


def parse_operator_ids(raw: str) -> set[int]:
    try:
        return {int(value.strip()) for value in raw.split(",") if value.strip()}
    except ValueError as exc:
        raise RuntimeError("OPERATOR_IDS должен содержать только числовые Telegram ID") from exc


class HermesChatGPT:
    """Classify multilingual questions and translate grounded FAQ answers."""

    def __init__(
        self,
        faq: dict[str, str] = FAQ,
        faq_provider: Callable[[], dict[str, str]] | None = None,
        timeout: float = 60.0,
    ):
        self.faq = faq
        self.faq_provider = faq_provider
        self.timeout = timeout
        self.model = os.getenv("HERMES_MODEL")

    async def _run(self, prompt: str) -> str | None:
        command = ["hermes", "chat", "-Q", "-q", prompt, "--max-turns", "1"]
        if self.model:
            command.extend(["-m", self.model])
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except (OSError, asyncio.TimeoutError):
            LOGGER.exception("Hermes ChatGPT недоступен")
            return None
        if process.returncode != 0:
            LOGGER.error("Hermes завершился с кодом %s", process.returncode)
            return None
        response = stdout.decode(errors="replace").strip()
        return "\n".join(line for line in response.splitlines() if not line.startswith("session_id:")).strip()

    async def classify(self, question: str) -> tuple[str | None, str | None]:
        faq = self.faq_provider() if self.faq_provider else self.faq
        knowledge = "\n".join(f"- {q}: {a}" for q, a in faq.items())
        prompt = (
            "Ты классифицируешь вопросы клиентов службы поддержки на любом языке. "
            "Сравни смысл вопроса с базой знаний, учитывая синонимы, перефразирование "
            "и небольшие ошибки. Определи язык вопроса.\n"
            "При совпадении верни ровно две строки:\n"
            "MATCH: <точный ключ этого пункта>\nLANGUAGE: <название языка на английском>\n"
            "Если совпадения нет, верни ровно две строки:\n"
            "ESCALATE\nLANGUAGE: <название языка на английском>\n"
            "Не выдумывай совпадения.\n\n"
            f"БАЗА ЗНАНИЙ:\n{knowledge}\n\nВОПРОС КЛИЕНТА:\n{question}"
        )
        classification = await self._run(prompt)
        if not classification:
            return None, None
        language = next(
            (line.split(":", 1)[1].strip() for line in classification.splitlines()
             if line.upper().startswith("LANGUAGE:")),
            None,
        )
        if classification.upper().startswith("ESCALATE"):
            return None, language
        match_key = next(
            (line.split(":", 1)[1].strip() for line in classification.splitlines()
             if line.upper().startswith("MATCH:")),
            None,
        )
        return faq.get(match_key or ""), language

    async def translate(self, text: str, language: str | None) -> str | None:
        if not language:
            return None
        prompt = (
            f"Переведи следующий текст на язык {language}. Сохрани точный смысл, "
            "вежливый тон и формат. Верни только перевод, без пояснений.\n\n"
            f"ТЕКСТ:\n{text}"
        )
        return await self._run(prompt)

    async def answer_with_language(self, question: str) -> tuple[str | None, str | None]:
        canonical_answer, language = await self.classify(question)
        if not canonical_answer:
            return None, language
        return await self.translate(canonical_answer, language), language

    async def answer(self, question: str) -> str | None:
        answer, _language = await self.answer_with_language(question)
        return answer


def get_store(context: ContextTypes.DEFAULT_TYPE) -> SupportStore:
    return context.application.bot_data["support_store"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = "Здравствуйте! Задайте вопрос. Я отвечу по базе знаний, а сложные вопросы передам оператору."
    keyboard = None
    user = update.effective_user
    store = get_store(context)
    if user and ADMIN_MINI_APP_URL and store.get_employee_by_telegram_id(user.id) and not store.is_blocked("user", user.id):
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Открыть админ-панель", web_app=WebAppInfo(url=ADMIN_MINI_APP_URL))]]
        )
    await update.message.reply_text(text, reply_markup=keyboard)


def message_targets_bot(update: Update, bot_username: str | None) -> bool:
    """Private chats are always handled; groups require an @mention."""
    chat = update.effective_chat
    message = update.message
    if not chat or not message:
        return False
    if chat.type not in {"group", "supergroup"}:
        return True
    if not bot_username:
        return False
    text = message.text or ""
    return re.search(rf"@{re.escape(bot_username)}(?=\s|$|[,.!?;:])", text, re.IGNORECASE) is not None


def remove_bot_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text.strip()
    return re.sub(
        rf"@{re.escape(bot_username)}(?=\s|$|[,.!?;:])",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def request_id_from_reply(message) -> int | None:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "")
    match = re.search(r"Заявка №(\d+)", text)
    return int(match.group(1)) if match else None


def telegram_reply_kwargs(request) -> dict[str, int]:
    kwargs: dict[str, int] = {}
    if request.source_message_id is not None:
        kwargs["reply_to_message_id"] = request.source_message_id
    if request.message_thread_id is not None:
        kwargs["message_thread_id"] = request.message_thread_id
    return kwargs


async def send_operator_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: int, body: str) -> bool:
    if not update.effective_user:
        return False
    store = get_store(context)
    target = store.reply_to_request(request_id, update.effective_user.id, body)
    if not target:
        return False
    sent_message = await context.bot.send_message(
        chat_id=target.chat_id,
        text=f"Ответ сотрудника по заявке №{target.id}:\n{body.strip()}",
        **telegram_reply_kwargs(target),
    )
    store.link_latest_message(
        target.id,
        update.effective_user.id,
        body.strip(),
        target.chat_id,
        sent_message.message_id,
        getattr(sent_message, "message_thread_id", None),
    )
    return True


def route_reply_request(
    store: SupportStore,
    user_id: int,
    user_label: str,
    chat_id: int,
    chat_type: str,
    question: str,
    replied_message_id: int,
    source_message_id: int,
    message_thread_id: int | None,
):
    replied = store.find_message_by_telegram(chat_id, replied_message_id)
    if not replied or replied.author_role not in {"bot", "employee"}:
        return None
    parent = store.get(replied.request_id)
    if not parent:
        return None
    if parent.status == "closed":
        return store.create_request(
            user_id,
            user_label,
            chat_id,
            chat_type,
            question,
            source_message_id=source_message_id,
            message_thread_id=message_thread_id,
        )
    store.add_user_message(parent.id, user_id, question, source_message_id, message_thread_id)
    return parent


def route_incoming_request(
    store: SupportStore,
    user_id: int,
    user_label: str,
    chat_id: int,
    chat_type: str,
    question: str,
    source_message_id: int,
    message_thread_id: int | None,
):
    request = store.get_active_for_message(user_id, chat_id, chat_type)
    if request:
        store.add_user_message(request.id, user_id, question, source_message_id, message_thread_id)
        return request
    return store.create_request(
        user_id,
        user_label,
        chat_id,
        chat_type,
        question,
        source_message_id=source_message_id,
        message_thread_id=message_thread_id,
    )


POLICY_ERROR_MESSAGE = "Бот не может обрабатывать ваши заявки в связи с нарушением политики использования."


async def track_bot_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type not in {"group", "supergroup"}:
        return
    chat = update.effective_chat
    store = get_store(context)
    member = update.my_chat_member
    is_active = bool(member and member.new_chat_member.status not in {"left", "kicked"})
    store.register_entity("group", chat.id, chat.title or str(chat.id), chat.username, is_active=is_active)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    user = update.effective_user
    chat = update.effective_chat
    store = get_store(context)
    bot_username = context.bot.username
    is_reply = update.message.reply_to_message is not None
    if not is_reply and not message_targets_bot(update, bot_username):
        return
    if user.id in context.application.bot_data["operator_ids"] and is_reply:
        request_id = request_id_from_reply(update.message.reply_to_message)
        if request_id:
            try:
                sent = await send_operator_reply(update, context, request_id, update.message.text or "")
                await update.message.reply_text(
                    "Ответ сохранён и отправлен клиенту." if sent else "Заявка не найдена.",
                )
            except Exception:
                LOGGER.exception("Не удалось отправить ответ оператора через reply")
                await update.message.reply_text("Ответ сохранён, но не отправлен клиенту.")
            return
    label = user.username or user.full_name or str(user.id)
    store.register_entity("user", user.id, user.full_name or str(user.id), user.username)
    if chat.type in {"group", "supergroup"}:
        store.register_entity("group", chat.id, chat.title or str(chat.id), chat.username)
    question = remove_bot_mention(update.message.text or "", bot_username)
    if not question:
        return
    reply_kwargs = {}
    if chat.type in {"group", "supergroup"}:
        reply_kwargs["reply_to_message_id"] = update.message.message_id
    if store.is_entity_blocked("user", user.id) or (
        chat.type in {"group", "supergroup"} and store.is_entity_blocked("group", chat.id)
    ):
        await update.message.reply_text(POLICY_ERROR_MESSAGE, **reply_kwargs)
        return
    if is_reply:
        request = route_reply_request(
            store,
            user.id,
            label,
            chat.id,
            chat.type,
            question,
            update.message.reply_to_message.message_id,
            update.message.message_id,
            getattr(update.message, "message_thread_id", None),
        )
        if not request:
            return
    else:
        request = route_incoming_request(
            store,
            user.id,
            label,
            chat.id,
            chat.type,
            question,
            update.message.message_id,
            getattr(update.message, "message_thread_id", None),
        )
    answer, language = await context.application.bot_data["chatgpt"].answer_with_language(question)
    store.set_language(request.id, language)
    store.set_auto_answer(request.id, bool(answer))
    if answer:
        sent_message = await update.message.reply_text(answer, **reply_kwargs)
        store.add_message(
            request.id,
            context.bot.id,
            "bot",
            answer,
            chat.id,
            sent_message.message_id,
            getattr(sent_message, "message_thread_id", None),
        )
        store.set_status(request.id, "answered")
    else:
        store.set_status(request.id, "waiting_employee")
    if not answer:
        notice = (
            f"Заявка №{request.id}\nКлиент: {label} (ID {user.id})\n"
            f"Тип чата: {chat.type}\nЗапрос: {question}\n"
            "Автоматический ответ не сформирован; требуется ответ сотрудника.\n"
            f"Ответить в админке: http://127.0.0.1:8001/admin/requests/{request.id}"
        )
        for operator_id in context.application.bot_data["operator_ids"]:
            try:
                await context.bot.send_message(operator_id, notice)
            except Exception:
                LOGGER.exception("Не удалось уведомить оператора %s", operator_id)


async def reply_to_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in context.application.bot_data["operator_ids"]:
        await update.message.reply_text("Команда доступна только операторам.")
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /reply <номер обращения> <текст>")
        return
    ticket = get_store(context).get(int(context.args[0]))
    if not ticket:
        await update.message.reply_text("Заявка с таким номером не найдена.")
        return
    body = " ".join(context.args[1:])
    try:
        sent = await send_operator_reply(update, context, ticket.id, body)
    except Exception:
        LOGGER.exception("Не удалось отправить ответ оператора по заявке %s", ticket.id)
        await update.message.reply_text("Ответ сохранён, но не отправлен клиенту.")
        return
    await update.message.reply_text("Ответ сохранён и отправлен клиенту." if sent else "Заявка не найдена.")


async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    store = get_store(context)
    if update.effective_user.id in context.application.bot_data["operator_ids"]:
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Формат: /close_ticket <номер обращения>")
            return
        ticket = store.get(int(context.args[0]))
    else:
        ticket = store.latest_open_for_user(update.effective_user.id)
    if not ticket or not store.set_status(ticket.id, "closed"):
        await update.message.reply_text("Открытая заявка не найдена.")
        return
    await update.message.reply_text(f"Заявка №{ticket.id} закрыта.")
    if update.effective_user.id in context.application.bot_data["operator_ids"]:
        await context.bot.send_message(ticket.chat_id, f"Заявка №{ticket.id} закрыта сотрудником.")


def build_application(
    token: str,
    operator_ids: set[int],
    database_url: str,
) -> Application:
    faq_store = FAQStore(database_url)
    support_store = SupportStore(database_url)
    faq_store.init_schema()
    faq_store.seed_defaults()
    support_store.init_schema()
    application = Application.builder().token(token).build()
    application.bot_data.update(
        support_store=support_store,
        operator_ids=set(operator_ids),
        chatgpt=HermesChatGPT(faq_provider=faq_store.knowledge),
    )
    application.add_handler(ChatMemberHandler(track_bot_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reply", reply_to_ticket))
    application.add_handler(CommandHandler("close", close_ticket))
    application.add_handler(CommandHandler("close_ticket", close_ticket))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main() -> None:
    global ADMIN_MINI_APP_URL
    load_dotenv()
    ADMIN_MINI_APP_URL = os.getenv("ADMIN_MINI_APP_URL", "").strip()
    if ADMIN_MINI_APP_URL and not ADMIN_MINI_APP_URL.startswith("https://"):
        raise RuntimeError("ADMIN_MINI_APP_URL должен использовать HTTPS")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "replace_with_botfather_token":
        raise RuntimeError("Укажите TELEGRAM_BOT_TOKEN в файле .env")
    operators = parse_operator_ids(os.getenv("OPERATOR_IDS", ""))
    if not operators:
        raise RuntimeError("Укажите хотя бы один ID в OPERATOR_IDS")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Укажите DATABASE_URL для PostgreSQL FAQ")
    build_application(token, operators, database_url).run_polling()


if __name__ == "__main__":
    main()
