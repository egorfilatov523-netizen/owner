import asyncio
import os
import random
import re
import sqlite3
import time
from contextlib import closing
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
       CallbackQuery,
       InlineKeyboardButton,
       InlineKeyboardMarkup,
       KeyboardButton,
       Message,
       ReplyKeyboardMarkup,
)
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")
WARNING_DELETE_SECONDS = 7
BOT_USERNAME = ""

if not BOT_TOKEN:
       raise RuntimeError("Укажи BOT_TOKEN в .env")


# Запрещённые слова не задаются по умолчанию: каждый пользователь добавляет их сам.

LOOKALIKES = str.maketrans({
       "ё": "е",
       "a": "а", "c": "с", "e": "е", "o": "о",
       "p": "р", "x": "х", "y": "у", "k": "к",
       "m": "м", "t": "т", "h": "н", "b": "в",
       "0": "о", "3": "з",
})


# ============================================================
# DATABASE
# ============================================================

def db():
       conn = sqlite3.connect(DB_PATH, timeout=10)
       conn.row_factory = sqlite3.Row
       return conn


def init_db():
       with closing(db()) as conn:
           conn.executescript("""
           CREATE TABLE IF NOT EXISTS users (
               user_id INTEGER PRIMARY KEY,
               username TEXT,
               full_name TEXT,
               created_at REAL NOT NULL
           );

           CREATE TABLE IF NOT EXISTS chats (
               chat_id INTEGER PRIMARY KEY,
               title TEXT,
               chat_type TEXT NOT NULL,
               created_at REAL NOT NULL
           );

           -- Один пользователь может подключить несколько чатов.
           -- Несколько администраторов могут подключить один и тот же чат.
           CREATE TABLE IF NOT EXISTS connections (
               user_id INTEGER NOT NULL,
               chat_id INTEGER NOT NULL,
               created_at REAL NOT NULL,
               PRIMARY KEY (user_id, chat_id),
               FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
               FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
           );

           -- Запрещенные слова принадлежат конкретному пользователю.
           CREATE TABLE IF NOT EXISTS banned_words (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL,
               word TEXT NOT NULL,
               created_at REAL NOT NULL,
               UNIQUE(user_id, word),
               FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
           );

           CREATE TABLE IF NOT EXISTS giveaways (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               chat_id INTEGER NOT NULL,
               creator_id INTEGER NOT NULL,
               message_id INTEGER,
               prize TEXT NOT NULL,
               winners_count INTEGER NOT NULL,
               ends_at REAL NOT NULL,
               active INTEGER NOT NULL DEFAULT 1
           );

           CREATE TABLE IF NOT EXISTS giveaway_users (
               giveaway_id INTEGER NOT NULL,
               user_id INTEGER NOT NULL,
               username TEXT,
               PRIMARY KEY (giveaway_id, user_id)
           );

           CREATE TABLE IF NOT EXISTS giveaway_winners (
               giveaway_id INTEGER NOT NULL,
               user_id INTEGER NOT NULL,
               username TEXT,
               chance REAL NOT NULL,
               PRIMARY KEY (giveaway_id, user_id)
           );
           """)
           # Remove legacy built-in words from earlier versions; custom words remain.
           legacy_defaults = [
               "бля", "блять", "блядь", "бляди", "сука", "суки", "сучка",
               "хуй", "хуя", "хуе", "хер", "пизда", "пиздец", "пизду", "пизды",
               "ебать", "ебал", "ебан", "ебись", "заебал", "заебись", "охуел",
               "мудак", "мудила", "долбоеб", "гандон", "шлюха",
           ]
           conn.executemany(
               "DELETE FROM banned_words WHERE word=?",
               [(normalize(word),) for word in legacy_defaults],
           )
           conn.commit()


def ensure_user(user):
       if not user:
           return
       with closing(db()) as conn:
           conn.execute("""
               INSERT INTO users(user_id, username, full_name, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   full_name=excluded.full_name
           """, (user.id, user.username, user.full_name, time.time()))
           conn.commit()


def normalize(text: str) -> str:
       text = (text or "").lower().translate(LOOKALIKES)
       text = re.sub(r"[^а-яa-z0-9]+", " ", text)
       return re.sub(r"\s+", " ", text).strip()


def get_user_words(user_id: int) -> list[str]:
       with closing(db()) as conn:
           rows = conn.execute(
               "SELECT word FROM banned_words WHERE user_id=? ORDER BY word",
               (user_id,)
           ).fetchall()
       return [row["word"] for row in rows]


def get_chat_words(chat_id: int) -> list[str]:
       with closing(db()) as conn:
           rows = conn.execute("""
               SELECT DISTINCT bw.word
               FROM banned_words bw
               JOIN connections c ON c.user_id = bw.user_id
               WHERE c.chat_id = ?
               ORDER BY bw.word
           """, (chat_id,)).fetchall()
       return [row["word"] for row in rows]


def find_bad_word(text: str, words: list[str]) -> str | None:
       normalized = normalize(text)
       if not normalized:
           return None

       compact = normalized.replace(" ", "")

       for word in words:
           if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
               return word

           compact_word = word.replace(" ", "")
           if len(compact_word) >= 5 and compact_word in compact:
               return word

       return None


# ============================================================
# ACCESS / CHAT CONNECTIONS
# ============================================================

admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
       key = (chat_id, user_id)
       now = time.monotonic()

       cached = admin_cache.get(key)
       if cached and cached[1] > now:
           return cached[0]

       try:
           member = await bot.get_chat_member(chat_id, user_id)
           result = member.status in ("administrator", "creator")
           admin_cache[key] = (result, now + 30)
           return result
       except TelegramAPIError:
           return False


async def can_manage(message: Message) -> bool:
       user = message.from_user
       if not user:
           return False

       if message.chat.type in ("group", "supergroup"):
           return await is_group_admin(message.bot, message.chat.id, user.id)

       return True


async def can_manage_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
       return await is_group_admin(bot, chat_id, user_id)


def is_connected(user_id: int, chat_id: int) -> bool:
       with closing(db()) as conn:
           row = conn.execute(
               "SELECT 1 FROM connections WHERE user_id=? AND chat_id=?",
               (user_id, chat_id)
           ).fetchone()
       return bool(row)


def connect_user_to_chat(user_id: int, chat_id: int, title: str, chat_type: str):
       with closing(db()) as conn:
           conn.execute("""
               INSERT INTO chats(chat_id, title, chat_type, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   title=excluded.title,
                   chat_type=excluded.chat_type
           """, (chat_id, title, chat_type, time.time()))

           conn.execute("""
               INSERT OR IGNORE INTO connections(user_id, chat_id, created_at)
               VALUES (?, ?, ?)
           """, (user_id, chat_id, time.time()))
           conn.commit()


def disconnect_user_from_chat(user_id: int, chat_id: int):
       with closing(db()) as conn:
           conn.execute(
               "DELETE FROM connections WHERE user_id=? AND chat_id=?",
               (user_id, chat_id)
           )
           conn.commit()


def get_user_chats(user_id: int) -> list[sqlite3.Row]:
       with closing(db()) as conn:
           return conn.execute("""
               SELECT c.chat_id, c.title, c.chat_type
               FROM chats c
               JOIN connections x ON x.chat_id=c.chat_id
               WHERE x.user_id=?
               ORDER BY c.title
           """, (user_id,)).fetchall()


# ============================================================
# KEYBOARDS
# ============================================================

BTN_ADD = "➕ Добавить слово"
BTN_DEL = "🗑 Удалить слово"
BTN_WORDS = "📋 Мои слова"
BTN_CONNECT = "🔗 Подключить к чату"
BTN_DISCONNECT = "🔌 Отключить от чата"
BTN_GIVEAWAY = "🎉 Розыгрыш"
BTN_CHATS = "💬 Мои чаты"
BTN_CANCEL = "❌ Отмена"


def main_keyboard() -> ReplyKeyboardMarkup:
       return ReplyKeyboardMarkup(
           keyboard=[
               [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_DEL)],
               [KeyboardButton(text=BTN_WORDS), KeyboardButton(text=BTN_CHATS)],
               [KeyboardButton(text=BTN_CONNECT), KeyboardButton(text=BTN_DISCONNECT)],
               [KeyboardButton(text=BTN_GIVEAWAY)],
           ],
           resize_keyboard=True,
           is_persistent=True,
           input_field_placeholder="Выберите действие",
       )


def cancel_keyboard() -> ReplyKeyboardMarkup:
       return ReplyKeyboardMarkup(
           keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
           resize_keyboard=True,
       )


def chats_inline(chats: list[sqlite3.Row], prefix: str) -> InlineKeyboardMarkup:
       rows = []
       for chat in chats:
           title = chat["title"] or str(chat["chat_id"])
           rows.append([
               InlineKeyboardButton(
                   text=f"💬 {title[:35]}",
                   callback_data=f"{prefix}:{chat['chat_id']}"
               )
           ])
       return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# FSM
# ============================================================

class AddWordState(StatesGroup):
       waiting_word = State()


class DeleteWordState(StatesGroup):
       waiting_word = State()


class GiveawayState(StatesGroup):
       choosing_chat = State()
       waiting_minutes = State()
       waiting_winners = State()
       waiting_prize = State()


# ============================================================
# BOT
# ============================================================

router = Router()


async def send_home(message: Message):
       ensure_user(message.from_user)
       await message.answer(
           "🛡 <b>Бот модерации</b>\n\n"
           "Настройки хранятся отдельно для каждого пользователя.\n"
           "Подключите бота к чату, добавьте свои запрещённые слова — "
           "они будут работать в подключённых вами чатах.",
           reply_markup=main_keyboard(),
           parse_mode="HTML",
       )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
       await state.clear()
       ensure_user(message.from_user)

       # Кнопка результатов открывает личный чат с ботом по deep-link.
       parts = (message.text or "").split(maxsplit=1)
       payload = parts[1].strip() if len(parts) > 1 else ""
       if payload.startswith("results_") and message.chat.type == "private":
           try:
               giveaway_id = int(payload.removeprefix("results_"))
           except ValueError:
               await message.answer("Не удалось определить розыгрыш.")
               return
           await message.answer("🎰 Подготавливаю быструю рулетку…")
           await finish_giveaway(message.bot, giveaway_id, animate=True, result_chat_id=message.chat.id)
           return

       # /start в группе больше не используется для подключения.
       if message.chat.type in ("group", "supergroup"):
           await message.answer(
               "ℹ️ Этот чат подключается автоматически после добавления "
               "бота администратором. Команда /start не нужна."
           )
           return

       await send_home(message)


@router.message(Command("menu"))
async def menu_command(message: Message, state: FSMContext):
       await state.clear()
       await send_home(message)


# ============================================================
# WORDS
# ============================================================

@router.message(F.text == BTN_ADD)
async def add_word_start(message: Message, state: FSMContext):
       await state.set_state(AddWordState.waiting_word)
       await message.answer(
           "✍️ Отправьте запрещённое слово одним сообщением.\n\n"
           "Оно сохранится только в вашем списке.",
           reply_markup=cancel_keyboard(),
       )


@router.message(AddWordState.waiting_word)
async def add_word_finish(message: Message, state: FSMContext):
       if message.text == BTN_CANCEL:
           await state.clear()
           await send_home(message)
           return

       word = normalize(message.text or "")
       if not word:
           await message.answer("Введите непустое слово.")
           return

       ensure_user(message.from_user)

       with closing(db()) as conn:
           conn.execute("""
               INSERT OR IGNORE INTO banned_words(user_id, word, created_at)
               VALUES (?, ?, ?)
           """, (message.from_user.id, word, time.time()))
           conn.commit()

       await state.clear()
       await message.answer(
           f"✅ Слово <b>{word}</b> добавлено в ваш список.",
           parse_mode="HTML",
           reply_markup=main_keyboard(),
       )


@router.message(F.text == BTN_DEL)
async def delete_word_start(message: Message, state: FSMContext):
       await state.set_state(DeleteWordState.waiting_word)
       await message.answer(
           "🗑 Отправьте слово, которое нужно удалить из вашего списка.",
           reply_markup=cancel_keyboard(),
       )


@router.message(DeleteWordState.waiting_word)
async def delete_word_finish(message: Message, state: FSMContext):
       if message.text == BTN_CANCEL:
           await state.clear()
           await send_home(message)
           return

       word = normalize(message.text or "")
       if not word:
           await message.answer("Введите слово.")
           return

       with closing(db()) as conn:
           cur = conn.execute(
               "DELETE FROM banned_words WHERE user_id=? AND word=?",
               (message.from_user.id, word)
           )
           conn.commit()

       await state.clear()

       if cur.rowcount:
           text = f"✅ Слово <b>{word}</b> удалено."
       else:
           text = f"ℹ️ Слова <b>{word}</b> нет в вашем списке."

       await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


@router.message(F.text == BTN_WORDS)
async def show_words(message: Message):
       words = get_user_words(message.from_user.id)

       if not words:
           await message.answer(
               "📋 Ваш список пока пуст.\n"
               "Нажмите «➕ Добавить слово».",
               reply_markup=main_keyboard(),
           )
           return

       await message.answer(
           "📋 <b>Ваши запрещённые слова:</b>\n\n"
           + "\n".join(f"• {w}" for w in words),
           parse_mode="HTML",
           reply_markup=main_keyboard(),
       )


# ============================================================
# CONNECT / DISCONNECT
# ============================================================

@router.message(F.text == BTN_CONNECT)
async def connect_help(message: Message):
       me = await message.bot.get_me()
       group_url = f"https://t.me/{me.username}?startgroup=true"
       channel_url = f"https://t.me/{me.username}?startchannel&admin=post_messages"
       keyboard = InlineKeyboardMarkup(inline_keyboard=[
           [InlineKeyboardButton(text="➕ Добавить в группу", url=group_url)],
           [InlineKeyboardButton(text="📢 Добавить в канал", url=channel_url)],
       ])
       await message.answer(
           "🔗 <b>Подключение группы или канала</b>\n\n"
           "Для группы выдайте боту право удалять сообщения.\n"
           "Для канала выдайте право публиковать сообщения.\n\n"
           "После назначения администратором чат подключится автоматически.\n"
           "Розыгрыши можно публиковать и в каналах; фильтрация сообщений работает в группах "
           "и обсуждениях каналов.",
           parse_mode="HTML",
           reply_markup=keyboard,
       )

@router.message(F.text == BTN_CHATS)
async def my_chats(message: Message):
       chats = get_user_chats(message.from_user.id)

       if not chats:
           await message.answer(
               "💬 У вас пока нет подключённых чатов.\n"
               "Нажмите «🔗 Подключить к чату».",
               reply_markup=main_keyboard(),
           )
           return

       lines = ["💬 <b>Ваши подключённые чаты:</b>", ""]
       for chat in chats:
           lines.append(f"• {chat['title'] or chat['chat_id']}")

       await message.answer(
           "\n".join(lines),
           parse_mode="HTML",
           reply_markup=main_keyboard(),
       )


@router.message(F.text == BTN_DISCONNECT)
async def disconnect_start(message: Message):
       chats = get_user_chats(message.from_user.id)

       if not chats:
           await message.answer(
               "Нет подключённых чатов.",
               reply_markup=main_keyboard(),
           )
           return

       await message.answer(
           "🔌 Выберите чат, который хотите отключить:",
           reply_markup=chats_inline(chats, "disconnect"),
       )


@router.callback_query(F.data.startswith("disconnect:"))
async def disconnect_callback(callback: CallbackQuery):
       user_id = callback.from_user.id
       chat_id = int(callback.data.split(":", 1)[1])

       if not is_connected(user_id, chat_id):
           await callback.answer("Этот чат уже отключён.", show_alert=True)
           return

       disconnect_user_from_chat(user_id, chat_id)
       await callback.answer("Чат отключён.")
       await callback.message.edit_text("✅ Чат отключён от вашего профиля.")


# ============================================================
# GIVEAWAYS
# ============================================================

def giveaway_keyboard(giveaway_id: int):
       return InlineKeyboardMarkup(
           inline_keyboard=[
               [InlineKeyboardButton(text="🎉 Участвовать", callback_data=f"join:{giveaway_id}")],
               [InlineKeyboardButton(
                   text="🎰 Проверить результаты",
                   url=f"https://t.me/{BOT_USERNAME}?start=results_{giveaway_id}",
               )],
           ]
       )


@router.message(F.text == BTN_GIVEAWAY)
async def giveaway_start(message: Message, state: FSMContext):
       chats = get_user_chats(message.from_user.id)

       if not chats:
           await message.answer(
               "Сначала подключите хотя бы один чат.",
               reply_markup=main_keyboard(),
           )
           return

       await state.set_state(GiveawayState.choosing_chat)
       await message.answer(
           "🎉 Выберите чат для розыгрыша:",
           reply_markup=chats_inline(chats, "givechat"),
       )


@router.callback_query(F.data.startswith("givechat:"))
async def giveaway_choose_chat(callback: CallbackQuery, state: FSMContext):
       chat_id = int(callback.data.split(":", 1)[1])

       if not is_connected(callback.from_user.id, chat_id):
           await callback.answer("Чат не подключён.", show_alert=True)
           return

       await state.update_data(chat_id=chat_id)
       await state.set_state(GiveawayState.waiting_minutes)
       await callback.answer()
       await callback.message.edit_text(
           "⏳ На сколько минут запустить розыгрыш?\n"
           "Например: <code>60</code>",
           parse_mode="HTML",
       )


@router.message(GiveawayState.waiting_minutes)
async def giveaway_minutes(message: Message, state: FSMContext):
       if message.text == BTN_CANCEL:
           await state.clear()
           await send_home(message)
           return

       if not (message.text or "").strip().isdigit():
           await message.answer("Введите количество минут числом.")
           return

       minutes = int(message.text)
       if not 1 <= minutes <= 10080:
           await message.answer("Допустимо от 1 до 10080 минут.")
           return

       await state.update_data(minutes=minutes)
       await state.set_state(GiveawayState.waiting_winners)
       await message.answer("🏆 Сколько будет победителей?")


@router.message(GiveawayState.waiting_winners)
async def giveaway_winners(message: Message, state: FSMContext):
       if not (message.text or "").strip().isdigit():
           await message.answer("Введите количество победителей числом.")
           return

       count = int(message.text)
       if not 1 <= count <= 50:
           await message.answer("Допустимо от 1 до 50 победителей.")
           return

       await state.update_data(winners=count)
       await state.set_state(GiveawayState.waiting_prize)
       await message.answer("🎁 Напишите приз.")


@router.message(GiveawayState.waiting_prize)
async def giveaway_prize(message: Message, state: FSMContext):
       prize = (message.text or "").strip()
       if not prize:
           await message.answer("Напишите название приза.")
           return

       data = await state.get_data()
       chat_id = data["chat_id"]
       minutes = data["minutes"]
       winners_count = data["winners"]

       # Проверяем права непосредственно перед созданием.
       if not await can_manage_chat(message.bot, chat_id, message.from_user.id):
           await state.clear()
           await message.answer(
               "⛔ Вы больше не являетесь администратором этого чата.",
               reply_markup=main_keyboard(),
           )
           return

       ends_at = time.time() + minutes * 60

       with closing(db()) as conn:
           cur = conn.execute("""
               INSERT INTO giveaways(
                   chat_id, creator_id, prize, winners_count, ends_at, active
               )
               VALUES (?, ?, ?, ?, ?, 1)
           """, (
               chat_id,
               message.from_user.id,
               prize,
               winners_count,
               ends_at,
           ))
           giveaway_id = cur.lastrowid
           conn.commit()

       try:
           sent = await message.bot.send_message(
               chat_id,
               f"🎉 <b>РОЗЫГРЫШ №{giveaway_id}</b>\n\n"
               f"🎁 Приз: {escape(prize)}\n"
               f"🏆 Победителей: {winners_count}\n"
               f"⏳ Завершение через: {minutes} мин.\n\n"
               "Нажмите кнопку ниже, чтобы участвовать!",
               parse_mode="HTML",
               reply_markup=giveaway_keyboard(giveaway_id),
           )

           with closing(db()) as conn:
               conn.execute(
                   "UPDATE giveaways SET message_id=? WHERE id=?",
                   (sent.message_id, giveaway_id),
               )
               conn.commit()

           asyncio.create_task(
               finish_giveaway_later(message.bot, giveaway_id, minutes * 60)
           )

           await state.clear()
           await message.answer(
               "✅ Розыгрыш опубликован в чате.",
               reply_markup=main_keyboard(),
           )

       except TelegramAPIError as e:
           with closing(db()) as conn:
               conn.execute(
                   "UPDATE giveaways SET active=0 WHERE id=?",
                   (giveaway_id,),
               )
               conn.commit()

           await state.clear()
           await message.answer(
               f"❌ Не удалось опубликовать розыгрыш.\n{e}",
               reply_markup=main_keyboard(),
           )


@router.callback_query(F.data.startswith("join:"))
async def join_giveaway(callback: CallbackQuery):
       try:
           giveaway_id = int(callback.data.split(":", 1)[1])
       except (ValueError, AttributeError):
           await callback.answer("Не удалось определить розыгрыш.", show_alert=True)
           return

       with closing(db()) as conn:
           giveaway = conn.execute(
               "SELECT * FROM giveaways WHERE id=? AND active=1", (giveaway_id,)
           ).fetchone()
           if not giveaway or giveaway["ends_at"] <= time.time():
               await callback.answer("Розыгрыш уже завершён.", show_alert=True)
               return

           cur = conn.execute("""
               INSERT OR IGNORE INTO giveaway_users(giveaway_id, user_id, username)
               VALUES (?, ?, ?)
           """, (giveaway_id, callback.from_user.id,
                 callback.from_user.username or callback.from_user.full_name))
           count = conn.execute(
               "SELECT COUNT(*) FROM giveaway_users WHERE giveaway_id=?", (giveaway_id,)
           ).fetchone()[0]
           conn.commit()

       if cur.rowcount:
           await callback.answer(f"✅ Вы участвуете! Участников: {count}", show_alert=True)
       else:
           await callback.answer(f"Вы уже участвуете! Всего участников: {count}", show_alert=True)


@router.callback_query(F.data.startswith("results:"))
async def giveaway_results_callback(callback: CallbackQuery):
       # Совместимость со старыми сообщениями розыгрыша.
       giveaway_id = int(callback.data.split(":", 1)[1])
       me = await callback.bot.get_me()
       await callback.answer("Открываю результаты в личном чате…", show_alert=False)
       await callback.message.answer(
           "🎰 Нажмите, чтобы открыть рулетку в боте:",
           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
               InlineKeyboardButton(
                   text="🎰 Открыть рулетку",
                   url=f"https://t.me/{me.username}?start=results_{giveaway_id}",
               )
           ]]),
       )


async def finish_giveaway_later(bot: Bot, giveaway_id: int, delay: int):
    """Automatically finalize the giveaway when its timer expires."""
    await asyncio.sleep(delay)
    with closing(db()) as conn:
        giveaway = conn.execute(
            "SELECT active FROM giveaways WHERE id=?", (giveaway_id,)
        ).fetchone()
    if not giveaway or not giveaway["active"]:
        return
    # Save winners and publish the final result to the original chat.
    await finish_giveaway(bot, giveaway_id, animate=False)


async def finish_giveaway(
    bot: Bot, giveaway_id: int, animate: bool = False,
    result_chat_id: int | None = None
):
    """
    Finalize winners only once. Each person opening /start results_ID gets
    their own DM roulette animation; saved winners never change.
    """
    with closing(db()) as conn:
        giveaway = conn.execute(
            "SELECT * FROM giveaways WHERE id=?", (giveaway_id,)
        ).fetchone()
        if not giveaway:
            if result_chat_id:
                await bot.send_message(result_chat_id, "❌ Розыгрыш не найден.")
            return

        participants = conn.execute(
            "SELECT user_id, username FROM giveaway_users "
            "WHERE giveaway_id=? ORDER BY user_id", (giveaway_id,)
        ).fetchall()
        users_count = len(participants)
        saved_winners = conn.execute(
            "SELECT user_id, username, chance FROM giveaway_winners "
            "WHERE giveaway_id=? ORDER BY rowid", (giveaway_id,)
        ).fetchall()

        # Results may only be calculated once the timer has ended.
        if not saved_winners and giveaway["ends_at"] > time.time():
            if result_chat_id:
                await bot.send_message(
                    result_chat_id,
                    "⏳ Розыгрыш ещё идёт. Проверьте результаты после его окончания."
                )
            return

        if not saved_winners and not participants:
            conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (giveaway_id,))
            conn.commit()
            no_entries = True
            winners = []
        else:
            no_entries = False
            if saved_winners:
                winners = [dict(row) for row in saved_winners]
            else:
                winners_count = min(int(giveaway["winners_count"]), users_count)
                chance = winners_count / users_count * 100.0
                picked = random.sample(list(participants), winners_count)
                winners = []
                for winner in picked:
                    item = {
                        "user_id": winner["user_id"],
                        "username": winner["username"],
                        "chance": chance,
                    }
                    winners.append(item)
                    conn.execute(
                        "INSERT INTO giveaway_winners "
                        "(giveaway_id,user_id,username,chance) VALUES (?,?,?,?)",
                        (giveaway_id, item["user_id"], item["username"], chance),
                    )
                conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (giveaway_id,))
                conn.commit()

    target_chat = result_chat_id or giveaway["chat_id"]

    if no_entries:
        message = (
            f"🎁 <b>Розыгрыш №{giveaway_id} завершён.</b>\n\n"
            "В этот раз никто не участвовал."
        )
        try:
            await bot.send_message(target_chat, message, parse_mode="HTML")
        except TelegramAPIError:
            pass
        if result_chat_id and result_chat_id != giveaway["chat_id"]:
            try:
                await bot.send_message(giveaway["chat_id"], message, parse_mode="HTML")
            except TelegramAPIError:
                pass
        return

    def participant_label(person):
        username = person.get("username") if isinstance(person, dict) else person["username"]
        user_id = person.get("user_id") if isinstance(person, dict) else person["user_id"]
        if username:
            return "@" + username if " " not in username else username
        return str(user_id)

    chance = float(winners[0].get("chance", 0.0)) if winners else 0.0
    winner_lines = [
        f"{i}. <b>{escape(participant_label(winner))}</b> — вероятность выигрыша: "
        f"<b>{float(winner.get('chance', chance)):.1f}%</b>"
        for i, winner in enumerate(winners, 1)
    ]
    result = (
        f"🏁 <b>РОЗЫГРЫШ №{giveaway_id} ЗАВЕРШЁН!</b>\n\n"
        f"🎁 Приз: {escape(giveaway['prize'])}\n"
        f"👥 Участников: <b>{users_count}</b>\n"
        f"🎯 Вероятность попасть в число победителей: <b>{chance:.1f}%</b>\n\n"
        "🏆 <b>Победитель(и):</b>\n" + "\n".join(winner_lines)
    )

    if animate and result_chat_id:
        # A separate animation message is created for each user in their DM.
        try:
            animation_message = await bot.send_message(
                result_chat_id,
                f"🎰 <b>РУЛЕТКА РОЗЫГРЫША №{giveaway_id}</b>\n\n"
                f"🎁 Приз: {escape(giveaway['prize'])}\n"
                f"👥 Участников: <b>{users_count}</b>\n\n"
                "🔴  ⚫  🟢  ⚫  🔴\n\n"
                "🎯 Рулетка запускается…",
                parse_mode="HTML",
            )
            pool = list(participants)
            random.shuffle(pool)
            # Even when results were already saved, animate every button press.
            frame_count = min(7, max(3, len(pool)))
            for frame in range(frame_count):
                candidate = pool[frame % len(pool)] if pool else winners[frame % len(winners)]
                label = participant_label(candidate)
                frame_text = (
                    f"🎰 <b>РУЛЕТКА РОЗЫГРЫША №{giveaway_id}</b>\n\n"
                    f"🎁 Приз: {escape(giveaway['prize'])}\n\n"
                    "🔴  ⚫  🟢  ⚫  🔴\n\n"
                    f"🎯 Выпадает: <b>{escape(label)}</b>\n"
                    "<i>Рулетка крутится…</i>"
                )
                try:
                    await animation_message.edit_text(frame_text, parse_mode="HTML")
                except TelegramAPIError:
                    pass
                await asyncio.sleep(0.65)
            try:
                await animation_message.edit_text(result, parse_mode="HTML")
            except TelegramAPIError:
                await bot.send_message(result_chat_id, result, parse_mode="HTML")
        except TelegramAPIError:
            try:
                await bot.send_message(result_chat_id, result, parse_mode="HTML")
            except TelegramAPIError:
                pass
        return

    # Automatic completion publishes results in the group/channel.
    try:
        await bot.send_message(giveaway["chat_id"], result, parse_mode="HTML")
    except TelegramAPIError:
        pass


# ============================================================
# COMMAND COMPATIBILITY
# Старые команды оставлены, но основное управление — кнопками.
# ============================================================

@router.message(Command("addword"))
async def legacy_addword(message: Message):
       # Поддержка старой команды без дублирования логики FSM.
       if not await can_manage(message):
           await message.answer("⛔ Нет доступа.")
           return

       args = (message.text or "").split(maxsplit=1)
       if len(args) < 2:
           await message.answer("Используйте кнопку «➕ Добавить слово».")
           return

       word = normalize(args[1])
       with closing(db()) as conn:
           conn.execute(
               "INSERT OR IGNORE INTO banned_words(user_id, word, created_at) VALUES (?, ?, ?)",
               (message.from_user.id, word, time.time()),
           )
           conn.commit()

       await message.answer(f"✅ Добавлено: {word}")


@router.message(Command("delword"))
async def legacy_delword(message: Message):
       if not await can_manage(message):
           await message.answer("⛔ Нет доступа.")
           return

       args = (message.text or "").split(maxsplit=1)
       if len(args) < 2:
           await message.answer("Используйте кнопку «🗑 Удалить слово».")
           return

       word = normalize(args[1])
       with closing(db()) as conn:
           cur = conn.execute(
               "DELETE FROM banned_words WHERE user_id=? AND word=?",
               (message.from_user.id, word),
           )
           conn.commit()

       await message.answer(
           "✅ Удалено." if cur.rowcount else "Такого слова нет в вашем списке."
       )


# ============================================================
# AUTO MODERATION
# ============================================================

async def delete_later(message: Message, delay: int = WARNING_DELETE_SECONDS):
       await asyncio.sleep(delay)
       try:
           await message.delete()
       except TelegramAPIError:
           pass


async def notify_connected_admins(
       bot: Bot,
       chat_id: int,
       chat_title: str,
       username: str,
       user_id: int,
       bad_word: str,
       original_text: str,
):
       with closing(db()) as conn:
           admins = conn.execute(
               "SELECT user_id FROM connections WHERE chat_id=?",
               (chat_id,),
           ).fetchall()

       report = (
           "🚨 <b>Нарушение правил</b>\n\n"
           f"Группа: {chat_title}\n"
           f"Пользователь: {username}\n"
           f"ID: {user_id}\n"
           f"Слово: {bad_word}\n"
           f"Сообщение: {original_text[:1000]}"
       )

       for row in admins:
           try:
               await bot.send_message(row["user_id"], report, parse_mode="HTML")
           except TelegramAPIError:
               pass


# ============================================================
# АВТОПОДКЛЮЧЕНИЕ ЧАТА — БЕЗ /start
# ============================================================

@router.my_chat_member()
async def bot_chat_member_updated(event):
       chat = event.chat
       actor = event.from_user
       new_status = event.new_chat_member.status

       if chat.type not in ("group", "supergroup", "channel") or not actor:
           return

       ensure_user(actor)

       if new_status in ("left", "kicked"):
           disconnect_user_from_chat(actor.id, chat.id)
           return

       if new_status == "member" and chat.type != "channel":
           try:
               await event.bot.send_message(
                   actor.id,
                   f"⚠️ Бот добавлен в «{chat.title or chat.id}», "
                   "но для модерации ему нужны права администратора "
                   "с правом удаления сообщений."
               )
           except TelegramAPIError:
               pass
           return

       if new_status == "administrator":
           if not await is_group_admin(event.bot, chat.id, actor.id):
               return

           required_permission = (
               getattr(event.new_chat_member, "can_post_messages", False)
               if chat.type == "channel"
               else getattr(event.new_chat_member, "can_delete_messages", False)
           )

           if not required_permission:
               try:
                   explanation = (
                       "У бота нет права публиковать сообщения. Включите право «Публикация сообщений»."
                       if chat.type == "channel"
                       else "У бота нет права удалять сообщения. Включите это право."
                   )
                   await event.bot.send_message(
                       actor.id,
                       f"⚠️ Бот назначен администратором «{chat.title or chat.id}».\n{explanation}"
                   )
               except TelegramAPIError:
                   pass
               return

           connect_user_to_chat(
               actor.id,
               chat.id,
               chat.title or str(chat.id),
               chat.type,
           )

           try:
               await event.bot.send_message(
                   actor.id,
                   f"✅ <b>Чат подключён!</b>\n\n"
                   f"💬 {chat.title or chat.id}\n"
                   + ("📢 Бот подключён к каналу и может публиковать посты/розыгрыши.\n\n"
                      if chat.type == "channel" else "🛡 Модерация включена.\n\n")
                   + "Теперь настройте слова через кнопку "
                   "«➕ Добавить слово».",
                   parse_mode="HTML",
                   reply_markup=main_keyboard(),
               )
           except TelegramAPIError:
               pass


@router.message()
async def moderate(message: Message):
       if message.chat.type not in ("group", "supergroup"):
           return

       user = message.from_user
       if not user or user.is_bot:
           return

       # Команды и служебные сообщения не фильтруем.
       text = message.text or message.caption or ""
       if not text or text.startswith("/"):
           return

       # Администраторы группы не модерируются.
       if await is_group_admin(message.bot, message.chat.id, user.id):
           return

       words = get_chat_words(message.chat.id)
       if not words:
           return

       bad_word = find_bad_word(text, words)
       if not bad_word:
           return

       try:
           await message.delete()
       except TelegramAPIError:
           # Боту обязательно нужно право Delete messages.
           return

       username = (
           f"@{user.username}"
           if user.username
           else user.full_name
       )

       try:
           warning = await message.answer(
               f"⚠️ {username}, ваше сообщение было удалено.\n"
               "Причина: нарушение правил модерации."
           )
           asyncio.create_task(delete_later(warning))
       except TelegramAPIError:
           pass

       asyncio.create_task(
           notify_connected_admins(
               bot=message.bot,
               chat_id=message.chat.id,
               chat_title=message.chat.title or str(message.chat.id),
               username=username,
               user_id=user.id,
               bad_word=bad_word,
               original_text=text,
           )
       )


# ============================================================
# START
# ============================================================

async def main():
       init_db()

       bot = Bot(BOT_TOKEN)
       global BOT_USERNAME
       BOT_USERNAME = (await bot.get_me()).username or ""
       dp = Dispatcher()
       dp.include_router(router)

       print("Бот модерации запущен.")
       print(f"DB: {DB_PATH}")

       try:
           await dp.start_polling(bot)
       finally:
           await bot.session.close()


if __name__ == "__main__":
       asyncio.run(main())
