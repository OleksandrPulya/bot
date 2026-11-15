import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

TOKEN = "9047816471:AAEW4r3XLu8XKFWlaJETOrXfR90JL2azVnc"

logging.basicConfig(level=logging.INFO)
router = Router()
BOT_USERNAME = ""  # заповнюється під час запуску в main()

# ---------------------- ГОЛОСОВІ ПОВІДОМЛЕННЯ (Whisper) ----------------------
# Потрібно: pip install faster-whisper   (і системний ffmpeg для декодування .ogg)
# Модель багатомовна, авто-визначає мову — українську, російську й англійську
# розпізнає "з коробки", без додаткових налаштувань.
_whisper_model = None


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model


async def transcribe_voice(bot: Bot, file_id: str):
    file = await bot.get_file(file_id)
    tmp_path = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False).name
    await bot.download_file(file.file_path, tmp_path)

    def _run():
        model = get_whisper_model()
        segments, info = model.transcribe(tmp_path, language=None, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text, info.language

    try:
        text, lang = await asyncio.to_thread(_run)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    return text, lang


# ---------------------- БАЗА ДАНИХ ----------------------
db = sqlite3.connect("bot.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS connections(
    conn_id TEXT PRIMARY KEY, owner_id INTEGER, owner_chat_id INTEGER);
CREATE TABLE IF NOT EXISTS settings(
    owner_id INTEGER PRIMARY KEY,
    log_edits INTEGER DEFAULT 1,
    log_deletes INTEGER DEFAULT 1,
    log_media INTEGER DEFAULT 1,
    delete_cmd INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS muted(
    owner_id INTEGER, chat_id INTEGER, PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS messages(
    conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
    from_name TEXT, text TEXT, media_type TEXT, file_id TEXT,
    PRIMARY KEY(conn_id, chat_id, msg_id));
CREATE TABLE IF NOT EXISTS ignored_chats(
    owner_id INTEGER, chat_id INTEGER, PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS keywords(
    owner_id INTEGER, word TEXT, PRIMARY KEY(owner_id, word));
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER, text TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS scheduled_tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER, chat_id INTEGER, conn_id TEXT,
    type TEXT, run_at REAL, payload TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS daily_tasks(
    owner_id INTEGER, task TEXT, last_date TEXT,
    PRIMARY KEY(owner_id, task));
CREATE TABLE IF NOT EXISTS stats_log(
    owner_id INTEGER, event TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS autoreply_sent(
    owner_id INTEGER, chat_id INTEGER, date TEXT,
    PRIMARY KEY(owner_id, chat_id, date));
CREATE TABLE IF NOT EXISTS templates(
    owner_id INTEGER, name TEXT, text TEXT, PRIMARY KEY(owner_id, name));
CREATE TABLE IF NOT EXISTS message_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT, chat_id INTEGER, msg_id INTEGER, text TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS keyword_scope(
    owner_id INTEGER, word TEXT, chat_id INTEGER, mode TEXT,
    PRIMARY KEY(owner_id, word, chat_id));
CREATE TABLE IF NOT EXISTS vip(
    owner_id INTEGER, chat_id INTEGER, PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS autoreply_contacts(
    owner_id INTEGER, chat_id INTEGER, text TEXT, PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS chat_names(
    owner_id INTEGER, chat_id INTEGER, name TEXT, PRIMARY KEY(owner_id, chat_id));
""")
db.commit()


def ensure_column(table: str, col: str, decl: str):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


ensure_column("muted", "until", "REAL")
ensure_column("muted", "chat_name", "TEXT")
ensure_column("messages", "created_at", "REAL DEFAULT 0")
ensure_column("messages", "from_id", "INTEGER")
ensure_column("messages", "from_username", "TEXT")
ensure_column("settings", "dnd_enabled", "INTEGER DEFAULT 0")
ensure_column("settings", "dnd_start", "TEXT DEFAULT '23:00'")
ensure_column("settings", "dnd_end", "TEXT DEFAULT '08:00'")
ensure_column("settings", "autoreply_enabled", "INTEGER DEFAULT 0")
ensure_column("settings", "autoreply_text", "TEXT DEFAULT ''")
ensure_column("settings", "autoreply_start", "TEXT DEFAULT ''")
ensure_column("settings", "autoreply_end", "TEXT DEFAULT ''")
ensure_column("settings", "cleanup_days", "INTEGER DEFAULT 30")
ensure_column("settings", "voice_transcribe_enabled", "INTEGER DEFAULT 0")
ensure_column("settings", "weekly_report_enabled", "INTEGER DEFAULT 0")
ensure_column("scheduled_tasks", "recurrence", "TEXT")
ensure_column("scheduled_tasks", "weekday", "INTEGER")
ensure_column("scheduled_tasks", "hhmm", "TEXT")
ensure_column("stats_log", "chat_id", "INTEGER")
db.commit()

SETTING_TITLES = {
    "log_edits": "Сповіщати про редагування",
    "log_deletes": "Сповіщати про видалення",
    "log_media": "Пересилати медіа при видаленні",
    "delete_cmd": "Видаляти мою команду !мут",
    "dnd_enabled": "Режим «не турбувати»",
    "autoreply_enabled": "Автовідповідь",
    "voice_transcribe_enabled": "🎙 Розпізнавання голосових",
    "weekly_report_enabled": "📊 Тижневий звіт",
}

DURATION_RE = re.compile(r"(\d+)\s*(хв|м|г|д)", re.IGNORECASE)
UNIT_SECONDS = {"хв": 60, "м": 60, "г": 3600, "д": 86400}
TIME_RANGE_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")
WEEKDAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "нд": 6}
WEEKDAY_NAMES = {v: k for k, v in WEEKDAYS.items()}


def parse_duration(text: str):
    m = DURATION_RE.fullmatch(text.strip().lower())
    if not m:
        return None
    n, unit = m.groups()
    return int(n) * UNIT_SECONDS[unit]


def next_occurrence(hhmm: str):
    try:
        hh, mm = map(int, hhmm.strip().split(":"))
    except Exception:
        return None
    now = datetime.now()
    run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run.timestamp()


def next_weekday_occurrence(weekday: int, hhmm: str):
    try:
        hh, mm = map(int, hhmm.strip().split(":"))
    except Exception:
        return None
    now = datetime.now()
    days_ahead = (weekday - now.weekday()) % 7
    run = (now + timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=7)
    return run.timestamp()


def parse_reminder_command(rest: str):
    """Повертає (recurrence, weekday|None, hhmm, text) або None."""
    rest = rest.strip()
    if not rest:
        return None
    low = rest.lower()
    try:
        if low.startswith("щодня "):
            _, hhmm, text = rest.split(maxsplit=2)
            return "daily", None, hhmm, text
        first_word = rest.split(maxsplit=1)[0].lower()
        if first_word in WEEKDAYS:
            _, hhmm, text = rest.split(maxsplit=2)
            return "weekly", WEEKDAYS[first_word], hhmm, text
        hhmm, text = rest.split(maxsplit=1)
        return "once", None, hhmm, text
    except ValueError:
        return None


def in_time_window(start: str, end: str) -> bool:
    if not start or not end:
        return True
    now = datetime.now().strftime("%H:%M")
    if start <= end:
        return start <= now < end
    return now >= start or now < end


# ---------------------- ТЕКСТИ ІНСТРУКЦІЙ ----------------------
HELP_PAGES = {
    "connect": (
        "📖 <b>Як підключити бота</b>\n\n"
        "<b>1.</b> Онови Telegram до останньої версії. Якщо в налаштуваннях "
        "акаунта немає розділу «Автоматизація чатів», можливо, функція "
        "недоступна для твого акаунта.\n\n"
        "<b>2.</b> Натисни <b>/start</b> у цьому чаті. Без цього бот не зможе "
        "надсилати тобі сповіщення.\n\n"
        "<b>3.</b> Відкрий <b>Налаштування → Акаунт → Автоматизація чатів</b>.\n\n"
        "<b>4.</b> Введи юзернейм цього бота: <code>@{bot_username}</code>.\n\n"
        "<b>5.</b> Обери, до яких чатів бот матиме доступ (усі приватні чати "
        "або вибрані).\n\n"
        "<b>6.</b> Увімкни дозволи: <b>читати повідомлення</b> і "
        "<b>видаляти повідомлення</b>. Без другого мут не працюватиме.\n\n"
        "<b>7.</b> Підтверди. Бот надішле сюди повідомлення «Підключено»."
    ),
    "how": (
        "⚙️ <b>Як це працює</b>\n\n"
        "Після підключення бот отримує події з твоїх особистих чатів і:\n\n"
        "✏️ показує «Було / Стало» (і повну історію, якщо редагували кілька разів);\n"
        "🗑 пересилає видалені повідомлення й медіа, а якщо чат очистили "
        "повністю — надсилає текстовий файл з усією перепискою;\n"
        "🔇 мутить людей (назавжди або на час) командою <code>!мут</code>;\n"
        "⭐ VIP-контакти пробиваються навіть у режимі «не турбувати»;\n"
        "🙈 вміє повністю ігнорувати окремі чати;\n"
        "🔑 сповіщає про ключові слова (можна обмежити чи виключити чати);\n"
        "🎙 розпізнає голосові повідомлення (укр./рос./англ.);\n"
        "🤖 може автоматично відповідати, за розкладом або персонально;\n"
        "📝 зберігає нотатки, шаблони відповідей і нагадування (разові й "
        "повторювані);\n"
        "📨 вміє надсилати повідомлення з затримкою;\n"
        "📊 показує статистику, тижневий звіт, 🔎 пошук і 📤 експорт історії;\n"
        "🧹 сам чистить стару історію.\n\n"
        "<b>Обмеження</b>\n"
        "• Бот знає лише повідомлення, отримані <b>після</b> підключення.\n"
        "• Фото й відео <b>на один перегляд</b> бот не отримує.\n"
        "• Мут працює через видалення, тому повідомлення може на мить "
        "з'явитися.\n"
        "• Бот має працювати постійно, інакше події будуть втрачені."
    ),
    "cmds": (
        "🔇 <b>Команди в особистих чатах</b>\n"
        "(пиши їх співрозмовнику від свого імені)\n\n"
        "<code>!мут</code> / <code>!мут 30м</code> — замутити (назавжди / на час)\n"
        "<code>!анмут</code> — зняти мут\n"
        "<code>!вип</code> / <code>!анвип</code> — додати/зняти VIP (пробиває DND)\n"
        "<code>!ігнор</code> / <code>!деігнор</code> — ігнорувати чат / повернути\n"
        "<code>!нотатка текст</code> — зберегти нотатку\n"
        "<code>!нагадай 18:00 текст</code> — нагадати один раз\n"
        "<code>!нагадай щодня 18:00 текст</code> — нагадувати щодня\n"
        "<code>!нагадай пн 10:00 текст</code> — нагадувати щотижня (пн-нд)\n"
        "<code>!через 10м текст</code> — надіслати це повідомлення пізніше\n"
        "<code>!шаблон назва текст</code> — зберегти шаблон відповіді\n"
        "<code>!т назва</code> — підставити шаблон замість цієї команди\n"
        "<code>!ключлише слово</code> — ключове слово лише в цьому чаті\n"
        "<code>!ключвиняток слово</code> — вимкнути ключове слово в цьому чаті\n"
        "<code>!автовідповідь текст</code> / <code>off</code> — автовідповідь "
        "саме для цього контакту\n\n"
        "🤖 <b>Команди в чаті з ботом</b>\n\n"
        "/start, /help — меню й ця інструкція\n"
        "/settings — налаштування\n"
        "/stats — статистика за 7 днів\n"
        "/tasks — заплановані нагадування/повідомлення (зі скасуванням)\n"
        "/notes — список нотаток\n"
        "/templates — список шаблонів\n"
        "/search слово [@чат] [2026-11-01] — пошук серед збережених повідомлень\n"
        "/export — вивантажити історію у файл\n"
        "/keywords — список ключових слів\n"
        "/addkeyword слово — додати ключове слово\n"
        "/delkeyword слово — видалити ключове слово\n"
        "/autoreply текст — автовідповідь (<code>/autoreply off</code> — вимкнути,\n"
        "  <code>/autoreply 09:00-18:00 текст</code> — лише в цей час)\n"
        "/dnd 23:00-08:00 — тихі години (<code>/dnd off</code> — вимкнути)\n"
        "/cleanup 30 — скільки днів зберігати історію"
    ),
    "privacy": (
        "🔒 <b>Приватність</b>\n\n"
        "Щоб показувати зміни й видалення, бот <b>зберігає копії</b> "
        "повідомлень (зокрема всі версії редагувань), транскрипти голосових "
        "повідомлень, а також твої нотатки, шаблони, ключові слова й "
        "статистику використання у власній базі даних.\n\n"
        "• Дані зберігаються лише в базі цього бота й нікому не передаються, "
        "файл бази даних ніколи не надсилається в Telegram — ні тобі, ні "
        "будь-кому іншому.\n"
        "• Відключити бота можна будь-коли: <b>Налаштування → Акаунт → "
        "Автоматизація чатів</b>.\n"
        "• Стару історію бот видаляє сам (/cleanup), а кнопка нижче видаляє "
        "все одразу.\n\n"
        "<i>Чесно попереджай співрозмовників, що бот веде облік змін і "
        "видалень, якщо це не лише для особистого використання.</i>"
    ),
}

WELCOME = (
    "👋 <b>Привіт!</b>\n\n"
    "Я допомагаю стежити за змінами у твоїх особистих чатах: показую, що "
    "співрозмовник <b>видалив</b> або <b>відредагував</b>, вмію "
    "<b>мутити</b> людей і ще багато чого. Повний список — у «Команди».\n\n"
    "Щоб почати, відкрий «Як підключити». Кнопки знизу — для швидкого доступу."
)


# ---------------------- ДОПОМІЖНІ ФУНКЦІЇ БД ----------------------
def get_setting(owner_id: int, key: str) -> bool:
    db.execute("INSERT OR IGNORE INTO settings(owner_id) VALUES(?)", (owner_id,))
    row = db.execute(f"SELECT {key} FROM settings WHERE owner_id=?", (owner_id,)).fetchone()
    return bool(row[0])


def get_setting_value(owner_id: int, key: str):
    db.execute("INSERT OR IGNORE INTO settings(owner_id) VALUES(?)", (owner_id,))
    row = db.execute(f"SELECT {key} FROM settings WHERE owner_id=?", (owner_id,)).fetchone()
    return row[0] if row else None


def toggle_setting(owner_id: int, key: str):
    get_setting(owner_id, key)
    db.execute(f"UPDATE settings SET {key} = 1 - {key} WHERE owner_id=?", (owner_id,))
    db.commit()


def get_conn(conn_id: str):
    return db.execute(
        "SELECT owner_id, owner_chat_id FROM connections WHERE conn_id=?", (conn_id,)
    ).fetchone()


def is_muted(owner_id: int, chat_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM muted WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
    ).fetchone() is not None


def is_vip(owner_id: int, chat_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM vip WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
    ).fetchone() is not None


def in_dnd(owner_id: int) -> bool:
    if not get_setting(owner_id, "dnd_enabled"):
        return False
    row = db.execute(
        "SELECT dnd_start, dnd_end FROM settings WHERE owner_id=?", (owner_id,)
    ).fetchone()
    if not row:
        return False
    return in_time_window(row[0], row[1])


def allowed_now(owner_id: int, chat_id: int) -> bool:
    """Чи можна надсилати сповіщення власнику зараз (VIP пробиває DND)."""
    return is_vip(owner_id, chat_id) or not in_dnd(owner_id)


def get_chat_display_name(owner_id: int, chat_id: int) -> str:
    row = db.execute(
        "SELECT name FROM chat_names WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
    ).fetchone()
    return row[0] if row and row[0] else str(chat_id)


def format_sender(name: str, user_id, username: str = None) -> str:
    name = html.escape(name or "Невідомо")
    if username:
        return f"{name} (@{html.escape(username)}) [<code>{user_id}</code>]"
    return f"{name} [<code>{user_id}</code>]"


def extract_media(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    for kind in ("video", "animation", "voice", "video_note", "audio", "document", "sticker"):
        obj = getattr(m, kind)
        if obj:
            return kind, obj.file_id
    return None, None


def cache_message(m: Message):
    mtype, fid = extract_media(m)
    name = m.from_user.full_name if m.from_user else "Невідомо"
    user_id = m.from_user.id if m.from_user else None
    username = m.from_user.username if m.from_user else None
    text_value = m.text or m.caption
    now = time.time()
    db.execute(
        "INSERT OR REPLACE INTO messages"
        "(conn_id, chat_id, msg_id, from_name, text, media_type, file_id, "
        "created_at, from_id, from_username) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (m.business_connection_id, m.chat.id, m.message_id,
         name, text_value, mtype, fid, now, user_id, username),
    )
    db.execute(
        "INSERT INTO message_versions(conn_id, chat_id, msg_id, text, created_at) "
        "VALUES(?,?,?,?,?)",
        (m.business_connection_id, m.chat.id, m.message_id, text_value, now),
    )
    conn = get_conn(m.business_connection_id)
    if conn:
        owner_id = conn[0]
        db.execute(
            "INSERT OR REPLACE INTO chat_names(owner_id, chat_id, name) VALUES(?,?,?)",
            (owner_id, m.chat.id, m.chat.full_name or str(m.chat.id)),
        )
    db.commit()


# ---------------------- КЛАВІАТУРИ ----------------------
def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Як підключити", callback_data="help:connect")
    kb.button(text="⚙️ Як це працює", callback_data="help:how")
    kb.button(text="🔇 Команди", callback_data="help:cmds")
    kb.button(text="🔒 Приватність", callback_data="help:privacy")
    kb.button(text="🛠 Налаштування", callback_data="menu:settings")
    kb.adjust(1, 1, 2, 1)
    return kb.as_markup()


def main_reply_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🏠 Меню")
    kb.button(text="🛠 Налаштування")
    kb.button(text="📊 Статистика")
    kb.button(text="📝 Нотатки")
    kb.button(text="📋 Завдання")
    kb.button(text="❓ Довідка")
    kb.adjust(2, 2, 2)
    return kb.as_markup(resize_keyboard=True)


def back_keyboard(page: str = "") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if page == "privacy":
        kb.button(text="🧹 Видалити збережені копії", callback_data="wipe_messages")
    kb.button(text="« Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def settings_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, title in SETTING_TITLES.items():
        mark = "✅" if get_setting(owner_id, key) else "❌"
        kb.button(text=f"{mark} {title}", callback_data=f"tg:{key}")
    kb.button(text="📋 Список замучених", callback_data="menu:muted")
    kb.button(text="⭐ Список VIP", callback_data="menu:vip")
    kb.button(text="🧹 Скинути всі муті", callback_data="clear_mutes")
    kb.button(text="« Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def muted_keyboard(owner_id: int):
    rows = db.execute(
        "SELECT chat_id, chat_name, until FROM muted WHERE owner_id=?", (owner_id,)
    ).fetchall()
    kb = InlineKeyboardBuilder()
    if rows:
        text = "🔇 <b>Замучені контакти</b>\n\nНатисни, щоб зняти мут."
        for chat_id, name, until in rows:
            label = (name or str(chat_id))[:28]
            if until:
                label += " ⏳"
            kb.button(text=label, callback_data=f"unmute:{chat_id}")
    else:
        text = "Замучених немає."
    kb.button(text="« Назад", callback_data="menu:settings")
    kb.adjust(1)
    return text, kb.as_markup()


def vip_keyboard(owner_id: int):
    rows = db.execute(
        "SELECT v.chat_id, c.name FROM vip v LEFT JOIN chat_names c "
        "ON c.owner_id=v.owner_id AND c.chat_id=v.chat_id WHERE v.owner_id=?",
        (owner_id,),
    ).fetchall()
    kb = InlineKeyboardBuilder()
    if rows:
        text = "⭐ <b>VIP-контакти</b>\n\nНатисни, щоб зняти статус."
        for chat_id, name in rows:
            kb.button(text=(name or str(chat_id))[:28], callback_data=f"unvip:{chat_id}")
    else:
        text = "VIP-контактів немає. Додай командою <code>!вип</code> у чаті з людиною."
    kb.button(text="« Назад", callback_data="menu:settings")
    kb.adjust(1)
    return text, kb.as_markup()


def tasks_keyboard(owner_id: int):
    rows = db.execute(
        "SELECT id, type, run_at, payload, recurrence FROM scheduled_tasks "
        "WHERE owner_id=? AND done=0 ORDER BY run_at",
        (owner_id,),
    ).fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["📋 <b>Заплановані завдання</b>\n"]
    label_type = {
        "reminder": "⏰ Нагадування",
        "delayed_message": "📨 Відкладене повідомлення",
        "unmute": "🔊 Автозняття муту",
    }
    rec_mark = {"daily": " (щодня)", "weekly": " (щотижня)"}
    if rows:
        for tid, ttype, run_at, payload, recurrence in rows:
            when = datetime.fromtimestamp(run_at).strftime("%d.%m %H:%M")
            short = html.escape((payload or "")[:50])
            lines.append(
                f"#{tid} {label_type.get(ttype, ttype)}{rec_mark.get(recurrence, '')} — "
                f"{when}\n  {short}"
            )
            kb.button(text=f"❌ Скасувати #{tid}", callback_data=f"canceltask:{tid}")
    else:
        lines.append("Немає запланованих завдань.")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


# ---------------------- МЕНЮ В ЧАТІ З БОТОМ ----------------------
@router.message(Command("start", "help"))
async def cmd_start(m: Message):
    await m.answer(WELCOME, reply_markup=main_menu())
    await m.answer("Швидкі кнопки 👇", reply_markup=main_reply_keyboard())


@router.message(Command("settings"))
async def cmd_settings(m: Message):
    await m.answer(
        "🛠 <b>Налаштування</b>\n\nНатискай на пункт, щоб увімкнути чи вимкнути.",
        reply_markup=settings_keyboard(m.from_user.id),
    )


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    owner_id = m.from_user.id
    since = time.time() - 7 * 86400
    rows = db.execute(
        "SELECT event, COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=? GROUP BY event",
        (owner_id, since),
    ).fetchall()
    counts = {"received": 0, "edited": 0, "deleted": 0}
    counts.update(dict(rows))
    await m.answer(
        "📊 <b>За останні 7 днів</b>\n\n"
        f"Отримано повідомлень: {counts['received']}\n"
        f"Відредаговано: {counts['edited']}\n"
        f"Видалено: {counts['deleted']}"
    )


@router.message(Command("tasks"))
async def cmd_tasks(m: Message):
    text, kb = tasks_keyboard(m.from_user.id)
    await m.answer(text, reply_markup=kb)


@router.message(Command("templates"))
async def cmd_templates(m: Message):
    rows = db.execute(
        "SELECT name, text FROM templates WHERE owner_id=?", (m.from_user.id,)
    ).fetchall()
    if not rows:
        await m.answer(
            "Шаблонів ще немає. У будь-якому особистому чаті напиши "
            "<code>!шаблон назва текст</code>, а потім <code>!т назва</code>, "
            "щоб підставити його."
        )
        return
    lines = [f"• <b>{html.escape(n)}</b>: {html.escape(t[:80])}" for n, t in rows]
    await m.answer("📑 <b>Шаблони</b>\n\n" + "\n".join(lines))


@router.message(Command("notes"))
async def cmd_notes(m: Message):
    rows = db.execute(
        "SELECT text FROM notes WHERE owner_id=? ORDER BY created_at DESC LIMIT 20",
        (m.from_user.id,),
    ).fetchall()
    if not rows:
        await m.answer(
            "У тебе ще немає нотаток. Пиши <code>!нотатка текст</code> "
            "у будь-якому особистому чаті."
        )
        return
    lines = [f"• {html.escape(t)}" for (t,) in rows]
    await m.answer("📝 <b>Останні нотатки</b>\n\n" + "\n".join(lines))


@router.message(Command("search"))
async def cmd_search(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Напиши: <code>/search слово [@чат] [2026-11-01]</code>")
        return

    owner_id = m.from_user.id
    tokens = parts[1].split()
    date_token, chat_token, words = None, None, []
    for tok in tokens:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            date_token = tok
        elif tok.startswith("@"):
            chat_token = tok[1:]
        else:
            words.append(tok)
    query = " ".join(words)

    sql = (
        "SELECT from_name, text, chat_id FROM messages WHERE conn_id IN "
        "(SELECT conn_id FROM connections WHERE owner_id=?) AND text LIKE ?"
    )
    params = [owner_id, f"%{query}%"]

    if date_token:
        day_start = datetime.strptime(date_token, "%Y-%m-%d").timestamp()
        sql += " AND created_at BETWEEN ? AND ?"
        params += [day_start, day_start + 86400]

    if chat_token:
        chat_rows = db.execute(
            "SELECT chat_id FROM chat_names WHERE owner_id=? AND name LIKE ?",
            (owner_id, f"%{chat_token}%"),
        ).fetchall()
        chat_ids = [r[0] for r in chat_rows]
        if not chat_ids:
            await m.answer(f"Чат «{html.escape(chat_token)}» не знайдено серед збережених.")
            return
        sql += " AND chat_id IN (%s)" % ",".join("?" * len(chat_ids))
        params += chat_ids

    sql += " ORDER BY created_at DESC LIMIT 15"
    rows = db.execute(sql, params).fetchall()
    if not rows:
        await m.answer("Нічого не знайдено серед збережених повідомлень.")
        return
    lines = [
        f"• <b>{html.escape(n)}</b> ({html.escape(get_chat_display_name(owner_id, cid))}): "
        f"{html.escape((t or '')[:200])}"
        for n, t, cid in rows
    ]
    await m.answer("🔎 <b>Результати пошуку</b>\n\n" + "\n\n".join(lines))


@router.message(Command("export"))
async def cmd_export(m: Message):
    owner_id = m.from_user.id
    rows = db.execute(
        "SELECT from_name, text, media_type FROM messages WHERE conn_id IN "
        "(SELECT conn_id FROM connections WHERE owner_id=?) ORDER BY created_at",
        (owner_id,),
    ).fetchall()
    if not rows:
        await m.answer("Поки що немає збережених повідомлень для експорту.")
        return
    path = f"export_{owner_id}.txt"
    with open(path, "w", encoding="utf-8") as f:
        for name, text, mtype in rows:
            f.write(f"{name}: {text or f'[{mtype}]'}\n")
    await m.answer_document(FSInputFile(path), caption="📤 Експорт збережених повідомлень")
    os.remove(path)


@router.message(Command("keywords"))
async def cmd_keywords(m: Message):
    rows = db.execute("SELECT word FROM keywords WHERE owner_id=?", (m.from_user.id,)).fetchall()
    if not rows:
        await m.answer("Ключових слів ще немає. Додай: <code>/addkeyword слово</code>")
        return
    await m.answer(
        "🔑 <b>Ключові слова:</b>\n" + "\n".join(f"• {html.escape(w)}" for (w,) in rows)
    )


@router.message(Command("addkeyword"))
async def cmd_addkeyword(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Напиши: <code>/addkeyword слово</code>")
        return
    word = parts[1].strip()
    db.execute("INSERT OR IGNORE INTO keywords VALUES(?,?)", (m.from_user.id, word))
    db.commit()
    await m.answer(f"🔑 Додано ключове слово: «{html.escape(word)}»")


@router.message(Command("delkeyword"))
async def cmd_delkeyword(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Напиши: <code>/delkeyword слово</code>")
        return
    db.execute(
        "DELETE FROM keywords WHERE owner_id=? AND word=?",
        (m.from_user.id, parts[1].strip()),
    )
    db.commit()
    await m.answer("🗑 Видалено (якщо таке слово було).")


@router.message(Command("autoreply"))
async def cmd_autoreply(m: Message):
    parts = m.text.split(maxsplit=1)
    owner_id = m.from_user.id
    get_setting(owner_id, "autoreply_enabled")
    if len(parts) < 2:
        await m.answer(
            "Напиши: <code>/autoreply текст</code>, щоб задати автовідповідь, "
            "<code>/autoreply 09:00-18:00 текст</code>, щоб обмежити часом, "
            "або <code>/autoreply off</code>, щоб вимкнути."
        )
        return
    arg = parts[1].strip()
    if arg.lower() == "off":
        db.execute("UPDATE settings SET autoreply_enabled=0 WHERE owner_id=?", (owner_id,))
        await m.answer("🤖 Автовідповідь вимкнено.")
        db.commit()
        return

    first = arg.split(maxsplit=1)[0]
    if TIME_RANGE_RE.match(first) and " " in arg:
        start, end = first.split("-")
        body = arg.split(maxsplit=1)[1]
        db.execute(
            "UPDATE settings SET autoreply_enabled=1, autoreply_text=?, "
            "autoreply_start=?, autoreply_end=? WHERE owner_id=?",
            (body, start, end, owner_id),
        )
        await m.answer(f"🤖 Автовідповідь увімкнено ({start}–{end}):\n\n{html.escape(body)}")
    else:
        db.execute(
            "UPDATE settings SET autoreply_enabled=1, autoreply_text=?, "
            "autoreply_start='', autoreply_end='' WHERE owner_id=?",
            (arg, owner_id),
        )
        await m.answer(f"🤖 Автовідповідь увімкнено:\n\n{html.escape(arg)}")
    db.commit()


@router.message(Command("dnd"))
async def cmd_dnd(m: Message):
    parts = m.text.split(maxsplit=1)
    owner_id = m.from_user.id
    get_setting(owner_id, "dnd_enabled")
    if len(parts) < 2 or parts[1].strip().lower() == "off":
        db.execute("UPDATE settings SET dnd_enabled=0 WHERE owner_id=?", (owner_id,))
        db.commit()
        await m.answer("🌙 Режим «не турбувати» вимкнено.")
        return
    try:
        start, end = parts[1].strip().split("-")
        datetime.strptime(start, "%H:%M")
        datetime.strptime(end, "%H:%M")
    except Exception:
        await m.answer("Формат: <code>/dnd 23:00-08:00</code>")
        return
    db.execute(
        "UPDATE settings SET dnd_enabled=1, dnd_start=?, dnd_end=? WHERE owner_id=?",
        (start, end, owner_id),
    )
    db.commit()
    await m.answer(f"🌙 Режим «не турбувати»: {start}–{end}. VIP-контакти все одно проходять.")


@router.message(Command("cleanup"))
async def cmd_cleanup(m: Message):
    parts = m.text.split(maxsplit=1)
    owner_id = m.from_user.id
    get_setting(owner_id, "cleanup_days")
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await m.answer("Напиши: <code>/cleanup 30</code> (кількість днів зберігання)")
        return
    days = int(parts[1].strip())
    db.execute("UPDATE settings SET cleanup_days=? WHERE owner_id=?", (days, owner_id))
    db.commit()
    await m.answer(f"🧹 Збережені повідомлення видалятимуться через {days} дн.")


# ---------------------- КНОПКИ-ЯРЛИКИ ПІД ПОЛЕМ ВВОДУ ----------------------
@router.message(F.text == "🏠 Меню")
async def btn_menu(m: Message):
    await cmd_start(m)


@router.message(F.text == "🛠 Налаштування")
async def btn_settings(m: Message):
    await cmd_settings(m)


@router.message(F.text == "📊 Статистика")
async def btn_stats(m: Message):
    await cmd_stats(m)


@router.message(F.text == "📝 Нотатки")
async def btn_notes(m: Message):
    await cmd_notes(m)


@router.message(F.text == "📋 Завдання")
async def btn_tasks(m: Message):
    await cmd_tasks(m)


@router.message(F.text == "❓ Довідка")
async def btn_help(m: Message):
    await cmd_start(m)


# ---------------------- CALLBACK-КНОПКИ ----------------------
@router.callback_query(F.data == "menu:main")
async def cb_main(c: CallbackQuery):
    await c.message.edit_text(WELCOME, reply_markup=main_menu())
    await c.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(c: CallbackQuery):
    await c.message.edit_text(
        "🛠 <b>Налаштування</b>\n\nНатискай на пункт, щоб увімкнути чи вимкнути.",
        reply_markup=settings_keyboard(c.from_user.id),
    )
    await c.answer()


@router.callback_query(F.data == "menu:muted")
async def cb_muted(c: CallbackQuery):
    text, kb = muted_keyboard(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "menu:vip")
async def cb_vip(c: CallbackQuery):
    text, kb = vip_keyboard(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("unvip:"))
async def cb_unvip(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    db.execute("DELETE FROM vip WHERE owner_id=? AND chat_id=?", (c.from_user.id, chat_id))
    db.commit()
    text, kb = vip_keyboard(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer("VIP знято")


@router.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    db.execute("DELETE FROM muted WHERE owner_id=? AND chat_id=?", (c.from_user.id, chat_id))
    db.commit()
    text, kb = muted_keyboard(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer("Мут знято")


@router.callback_query(F.data.startswith("canceltask:"))
async def cb_canceltask(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    db.execute("UPDATE scheduled_tasks SET done=1 WHERE id=? AND owner_id=?", (tid, c.from_user.id))
    db.commit()
    text, kb = tasks_keyboard(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer("Скасовано")


@router.callback_query(F.data.startswith("help:"))
async def cb_help(c: CallbackQuery, bot: Bot):
    page = c.data.split(":")[1]
    text = HELP_PAGES[page].replace("{bot_username}", (await bot.me()).username)
    await c.message.edit_text(text, reply_markup=back_keyboard(page))
    await c.answer()


@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle(c: CallbackQuery):
    toggle_setting(c.from_user.id, c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=settings_keyboard(c.from_user.id))
    await c.answer("Збережено")


@router.callback_query(F.data == "clear_mutes")
async def cb_clear(c: CallbackQuery):
    db.execute("DELETE FROM muted WHERE owner_id=?", (c.from_user.id,))
    db.commit()
    await c.answer("Усі муті скинуто", show_alert=True)


@router.callback_query(F.data == "wipe_messages")
async def cb_wipe(c: CallbackQuery):
    db.execute(
        "DELETE FROM messages WHERE conn_id IN "
        "(SELECT conn_id FROM connections WHERE owner_id=?)",
        (c.from_user.id,),
    )
    db.execute(
        "DELETE FROM message_versions WHERE conn_id IN "
        "(SELECT conn_id FROM connections WHERE owner_id=?)",
        (c.from_user.id,),
    )
    db.commit()
    await c.answer("Збережені копії повідомлень видалено", show_alert=True)


# ---------------------- КОМАНДИ ВІД ВЛАСНИКА В ОСОБИСТИХ ЧАТАХ ----------------------
async def handle_owner_command(m: Message, bot: Bot, owner_id: int, owner_chat: int):
    text = (m.text or "").strip()
    lower = text.lower()
    handled = True

    if lower == "!мут" or lower.startswith("!мут "):
        parts = text.split(maxsplit=1)
        duration = parse_duration(parts[1]) if len(parts) > 1 else None
        db.execute(
            "INSERT OR REPLACE INTO muted(owner_id, chat_id, until, chat_name) VALUES(?,?,?,?)",
            (owner_id, m.chat.id, (time.time() + duration) if duration else None, m.chat.full_name),
        )
        if duration:
            db.execute(
                "INSERT INTO scheduled_tasks(owner_id, chat_id, conn_id, type, run_at, payload) "
                "VALUES(?,?,?,?,?,?)",
                (owner_id, m.chat.id, m.business_connection_id, "unmute",
                 time.time() + duration, None),
            )
            note = f"🔇 Замучено на {html.escape(parts[1])}: {html.escape(m.chat.full_name or str(m.chat.id))}"
        else:
            note = f"🔇 Замучено назавжди: {html.escape(m.chat.full_name or str(m.chat.id))}"
        db.commit()
        await bot.send_message(owner_chat, note)

    elif lower == "!анмут":
        db.execute("DELETE FROM muted WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id))
        db.commit()
        await bot.send_message(
            owner_chat, f"🔊 Мут знято: {html.escape(m.chat.full_name or str(m.chat.id))}"
        )

    elif lower == "!вип":
        db.execute("INSERT OR IGNORE INTO vip VALUES(?,?)", (owner_id, m.chat.id))
        db.commit()
        await bot.send_message(
            owner_chat,
            f"⭐ {html.escape(m.chat.full_name or str(m.chat.id))} тепер VIP — "
            "сповіщення проходитимуть навіть у «не турбувати».",
        )

    elif lower == "!анвип":
        db.execute("DELETE FROM vip WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id))
        db.commit()
        await bot.send_message(owner_chat, "⭐ Статус VIP знято.")

    elif lower == "!ігнор":
        db.execute("INSERT OR IGNORE INTO ignored_chats VALUES(?,?)", (owner_id, m.chat.id))
        db.commit()
        await bot.send_message(
            owner_chat,
            f"🙈 Чат додано до ігнорованих: {html.escape(m.chat.full_name or str(m.chat.id))}",
        )

    elif lower == "!деігнор":
        db.execute(
            "DELETE FROM ignored_chats WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id)
        )
        db.commit()
        await bot.send_message(owner_chat, "👁 Чат видалено з ігнорованих.")

    elif lower.startswith("!нотатка "):
        note_text = text.split(maxsplit=1)[1]
        db.execute(
            "INSERT INTO notes(owner_id, text, created_at) VALUES(?,?,?)",
            (owner_id, note_text, time.time()),
        )
        db.commit()
        await bot.send_message(owner_chat, f"📝 Нотатку збережено: {html.escape(note_text)}")

    elif lower.startswith("!нагадай "):
        rest = text.split(maxsplit=1)[1]
        parsed = parse_reminder_command(rest)
        if parsed is None:
            await bot.send_message(
                owner_chat,
                "Формат: <code>!нагадай 18:00 текст</code>, "
                "<code>!нагадай щодня 18:00 текст</code> або "
                "<code>!нагадай пн 10:00 текст</code>",
            )
        else:
            recurrence, weekday, hhmm, rtext = parsed
            if recurrence == "weekly":
                run_at = next_weekday_occurrence(weekday, hhmm)
            else:
                run_at = next_occurrence(hhmm)
            if run_at is None:
                await bot.send_message(owner_chat, "Не розпізнав час. Формат: <code>18:00</code>")
            else:
                db.execute(
                    "INSERT INTO scheduled_tasks"
                    "(owner_id, chat_id, conn_id, type, run_at, payload, recurrence, weekday, hhmm) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (owner_id, None, None, "reminder", run_at, rtext, recurrence, weekday, hhmm),
                )
                db.commit()
                when_label = {
                    "once": "один раз",
                    "daily": "щодня",
                    "weekly": f"щотижня в {WEEKDAY_NAMES.get(weekday, '?')}",
                }[recurrence]
                await bot.send_message(
                    owner_chat, f"⏰ Нагадаю ({when_label}, о {hhmm}): {html.escape(rtext)}"
                )

    elif lower.startswith("!через "):
        rest = text.split(maxsplit=1)[1]
        rparts = rest.split(maxsplit=1)
        duration = parse_duration(rparts[0]) if rparts else None
        if not duration or len(rparts) < 2:
            await bot.send_message(owner_chat, "Формат: <code>!через 10м текст</code>")
        else:
            db.execute(
                "INSERT INTO scheduled_tasks(owner_id, chat_id, conn_id, type, run_at, payload) "
                "VALUES(?,?,?,?,?,?)",
                (owner_id, m.chat.id, m.business_connection_id, "delayed_message",
                 time.time() + duration, rparts[1]),
            )
            db.commit()
            await bot.send_message(owner_chat, f"📨 Повідомлення надійде через {html.escape(rparts[0])}.")

    elif lower.startswith("!шаблон "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await bot.send_message(owner_chat, "Формат: <code>!шаблон назва текст</code>")
        else:
            _, name, body = parts
            db.execute("INSERT OR REPLACE INTO templates VALUES(?,?,?)", (owner_id, name, body))
            db.commit()
            await bot.send_message(owner_chat, f"💾 Шаблон «{html.escape(name)}» збережено.")

    elif lower.startswith("!т "):
        name = text.split(maxsplit=1)[1].strip()
        row = db.execute(
            "SELECT text FROM templates WHERE owner_id=? AND name=?", (owner_id, name)
        ).fetchone()
        if not row:
            await bot.send_message(owner_chat, f"Шаблон «{html.escape(name)}» не знайдено.")
        else:
            try:
                await bot.edit_message_text(
                    text=row[0],
                    chat_id=m.chat.id,
                    message_id=m.message_id,
                    business_connection_id=m.business_connection_id,
                )
            except Exception as e:
                logging.warning("Не вдалось підставити шаблон редагуванням: %s", e)
                try:
                    await bot.delete_business_messages(m.business_connection_id, [m.message_id])
                except Exception:
                    pass
                await bot.send_message(m.chat.id, row[0], business_connection_id=m.business_connection_id)
        return  # повідомлення вже замінено — не чіпаємо його далі

    elif lower.startswith("!ключлише "):
        word = text.split(maxsplit=1)[1].strip()
        db.execute(
            "INSERT OR REPLACE INTO keyword_scope VALUES(?,?,?,?)",
            (owner_id, word, m.chat.id, "only"),
        )
        db.commit()
        await bot.send_message(
            owner_chat, f"🔑 «{html.escape(word)}» тепер стежитиме лише за цим чатом."
        )

    elif lower.startswith("!ключвиняток "):
        word = text.split(maxsplit=1)[1].strip()
        db.execute(
            "INSERT OR REPLACE INTO keyword_scope VALUES(?,?,?,?)",
            (owner_id, word, m.chat.id, "exclude"),
        )
        db.commit()
        await bot.send_message(
            owner_chat, f"🔑 «{html.escape(word)}» вимкнено саме для цього чату."
        )

    elif lower.startswith("!автовідповідь"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await bot.send_message(
                owner_chat, "Формат: <code>!автовідповідь текст</code> або <code>off</code>"
            )
        else:
            arg = parts[1].strip()
            if arg.lower() == "off":
                db.execute(
                    "DELETE FROM autoreply_contacts WHERE owner_id=? AND chat_id=?",
                    (owner_id, m.chat.id),
                )
                await bot.send_message(
                    owner_chat, "🤖 Персональну автовідповідь для цього чату вимкнено "
                    "(діятиме загальна, якщо увімкнена)."
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO autoreply_contacts VALUES(?,?,?)",
                    (owner_id, m.chat.id, arg),
                )
                await bot.send_message(
                    owner_chat, f"🤖 Персональна автовідповідь для цього чату: {html.escape(arg)}"
                )
            db.commit()

    else:
        handled = False

    if handled and get_setting(owner_id, "delete_cmd"):
        try:
            await bot.delete_business_messages(m.business_connection_id, [m.message_id])
        except Exception:
            pass


# ---------------------- BUSINESS-ПОДІЇ ----------------------
@router.business_connection()
async def on_connection(c: BusinessConnection, bot: Bot):
    if c.is_enabled:
        db.execute(
            "INSERT OR REPLACE INTO connections VALUES(?,?,?)",
            (c.id, c.user.id, c.user_chat_id),
        )
        db.commit()
        try:
            await bot.send_message(
                c.user_chat_id,
                "✅ <b>Підключено!</b>\n\nНатисни /start, щоб подивитися, "
                "як це працює, і /settings, щоб налаштувати сповіщення.",
            )
        except Exception:
            pass


@router.business_message()
async def on_message(m: Message, bot: Bot):
    conn = get_conn(m.business_connection_id)
    if not conn:
        return
    owner_id, owner_chat = conn

    if m.from_user and m.from_user.id == owner_id:
        await handle_owner_command(m, bot, owner_id, owner_chat)
        return

    if db.execute(
        "SELECT 1 FROM ignored_chats WHERE owner_id=? AND chat_id=?",
        (owner_id, m.chat.id),
    ).fetchone():
        return

    if is_muted(owner_id, m.chat.id):
        await bot.delete_business_messages(m.business_connection_id, [m.message_id])
        return

    db.execute(
        "INSERT INTO stats_log(owner_id, event, ts, chat_id) VALUES(?,?,?,?)",
        (owner_id, "received", time.time(), m.chat.id),
    )
    db.commit()

    # Розпізнавання голосових повідомлень
    if m.voice and get_setting(owner_id, "voice_transcribe_enabled"):
        try:
            voice_text, lang = await transcribe_voice(bot, m.voice.file_id)
            if voice_text:
                await bot.send_message(
                    owner_chat,
                    f"🎙 Розпізнано ({html.escape(lang or '?')}), від "
                    f"{html.escape(m.from_user.full_name if m.from_user else 'Невідомо')}:\n\n"
                    f"{html.escape(voice_text)}",
                )
        except Exception as e:
            logging.warning("Помилка розпізнавання голосового: %s", e)

    content = m.text or m.caption or ""
    if content and allowed_now(owner_id, m.chat.id):
        words = db.execute("SELECT word FROM keywords WHERE owner_id=?", (owner_id,)).fetchall()
        low = content.lower()
        for (w,) in words:
            if w.lower() not in low:
                continue
            scopes = db.execute(
                "SELECT chat_id, mode FROM keyword_scope WHERE owner_id=? AND word=?",
                (owner_id, w),
            ).fetchall()
            only_chats = {cid for cid, mode in scopes if mode == "only"}
            excluded_chats = {cid for cid, mode in scopes if mode == "exclude"}
            if only_chats and m.chat.id not in only_chats:
                continue
            if m.chat.id in excluded_chats:
                continue
            await bot.send_message(
                owner_chat,
                f"🔑 Ключове слово «{html.escape(w)}» у повідомленні від "
                f"{html.escape(m.from_user.full_name)}:\n\n{html.escape(content)}",
            )
            break

    if get_setting(owner_id, "autoreply_enabled") and not in_dnd(owner_id):
        start = get_setting_value(owner_id, "autoreply_start") or ""
        end = get_setting_value(owner_id, "autoreply_end") or ""
        if in_time_window(start, end):
            today = datetime.now().strftime("%Y-%m-%d")
            sent = db.execute(
                "SELECT 1 FROM autoreply_sent WHERE owner_id=? AND chat_id=? AND date=?",
                (owner_id, m.chat.id, today),
            ).fetchone()
            if not sent:
                contact_row = db.execute(
                    "SELECT text FROM autoreply_contacts WHERE owner_id=? AND chat_id=?",
                    (owner_id, m.chat.id),
                ).fetchone()
                autoreply_text = contact_row[0] if contact_row else (
                    get_setting_value(owner_id, "autoreply_text") or ""
                )
                if autoreply_text:
                    try:
                        await bot.send_message(
                            m.chat.id, autoreply_text, business_connection_id=m.business_connection_id
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO autoreply_sent VALUES(?,?,?)",
                            (owner_id, m.chat.id, today),
                        )
                        db.commit()
                    except Exception as e:
                        logging.warning("Автовідповідь не надіслана: %s", e)

    cache_message(m)


@router.edited_business_message()
async def on_edited(m: Message, bot: Bot):
    conn = get_conn(m.business_connection_id)
    if not conn or (m.from_user and m.from_user.id == conn[0]):
        return
    owner_id, owner_chat = conn

    if db.execute(
        "SELECT 1 FROM ignored_chats WHERE owner_id=? AND chat_id=?",
        (owner_id, m.chat.id),
    ).fetchone():
        return

    prior_versions = db.execute(
        "SELECT text, created_at FROM message_versions "
        "WHERE conn_id=? AND chat_id=? AND msg_id=? ORDER BY created_at",
        (m.business_connection_id, m.chat.id, m.message_id),
    ).fetchall()

    db.execute(
        "INSERT INTO stats_log(owner_id, event, ts, chat_id) VALUES(?,?,?,?)",
        (owner_id, "edited", time.time(), m.chat.id),
    )
    db.commit()

    if get_setting(owner_id, "log_edits") and allowed_now(owner_id, m.chat.id):
        sender = format_sender(
            m.from_user.full_name if m.from_user else "Невідомо",
            m.from_user.id if m.from_user else "?",
            m.from_user.username if m.from_user else None,
        )
        footer = f"\n\n@{BOT_USERNAME}" if BOT_USERNAME else ""
        new_text = html.escape(m.text or m.caption or "—")

        if len(prior_versions) > 1:
            # Повідомлення редагували вже не вперше — показуємо всю історію версій
            lines = [f"{sender} знову змінив(ла) повідомлення. Історія версій:\n"]
            for i, (vtext, vts) in enumerate(prior_versions, start=1):
                ts_label = datetime.fromtimestamp(vts).strftime("%H:%M:%S")
                lines.append(
                    f"<b>Версія {i}</b> ({ts_label}):\n"
                    f"<blockquote>{html.escape(vtext) if vtext else '—'}</blockquote>"
                )
            lines.append(f"<b>Нова версія</b>:\n<blockquote>{new_text}</blockquote>{footer}")
            await bot.send_message(owner_chat, "\n\n".join(lines))
        else:
            old_text = html.escape(prior_versions[0][0]) if prior_versions and prior_versions[0][0] else (
                "<i>невідомо (до підключення бота)</i>"
            )
            await bot.send_message(
                owner_chat,
                f"{sender} змінив(ла) повідомлення:\n\n"
                f"Old:\n<blockquote>{old_text}</blockquote>\n\n"
                f"New:\n<blockquote>{new_text}</blockquote>"
                f"{footer}",
            )
    cache_message(m)


@router.deleted_business_messages()
async def on_deleted(d: BusinessMessagesDeleted, bot: Bot):
    conn = get_conn(d.business_connection_id)
    if not conn:
        return
    owner_id, owner_chat = conn

    if db.execute(
        "SELECT 1 FROM ignored_chats WHERE owner_id=? AND chat_id=?",
        (owner_id, d.chat.id),
    ).fetchone():
        return

    notify = get_setting(owner_id, "log_deletes") and allowed_now(owner_id, d.chat.id)
    deleted_rows = []

    for msg_id in d.message_ids:
        row = db.execute(
            "SELECT from_name, text, media_type, file_id, from_id, from_username "
            "FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (d.business_connection_id, d.chat.id, msg_id),
        ).fetchone()
        if not row:
            continue
        deleted_rows.append(row)

        db.execute(
            "INSERT INTO stats_log(owner_id, event, ts, chat_id) VALUES(?,?,?,?)",
            (owner_id, "deleted", time.time(), d.chat.id),
        )

        if notify:
            name, text, mtype, fid, sender_id, sender_username = row
            sender = format_sender(name, sender_id if sender_id else "?", sender_username)
            footer = f"\n\n@{BOT_USERNAME}" if BOT_USERNAME else ""
            header = f"{sender} видалив(ла) повідомлення:"
            if text:
                body = f"{header}\n\n<blockquote>{html.escape(text)}</blockquote>{footer}"
            else:
                body = f"{header}{footer}"

            if mtype and fid and get_setting(owner_id, "log_media"):
                try:
                    if mtype in ("video_note", "sticker"):
                        await bot.send_message(owner_chat, header + footer)
                        await getattr(bot, f"send_{mtype}")(owner_chat, fid)
                    else:
                        await getattr(bot, f"send_{mtype}")(
                            owner_chat, fid, caption=(header + footer)[:1024]
                        )
                except Exception as e:
                    logging.warning("Не вдалось переслати медіа: %s", e)
                    await bot.send_message(owner_chat, body + f"\n\n(медіа: {mtype})")
            else:
                await bot.send_message(owner_chat, body)

        db.execute(
            "DELETE FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (d.business_connection_id, d.chat.id, msg_id),
        )

    # Якщо після видалення в цього чату не лишилось жодного збереженого
    # повідомлення (весь чат очистили/видалили) — надсилаємо повну копію
    # перепсики текстовим файлом, поки вона остаточно не загубилась.
    if deleted_rows:
        remaining = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conn_id=? AND chat_id=?",
            (d.business_connection_id, d.chat.id),
        ).fetchone()[0]
        if remaining == 0:
            try:
                fname = f"chat_dump_{owner_id}_{d.chat.id}_{int(time.time())}.txt"
                with open(fname, "w", encoding="utf-8") as f:
                    for name, text, mtype, *_ in deleted_rows:
                        f.write(f"{name}: {text or f'[{mtype}]'}\n")
                await bot.send_document(
                    owner_chat,
                    FSInputFile(fname),
                    caption=(
                        f"🗑 Схоже, чат «{html.escape(get_chat_display_name(owner_id, d.chat.id))}» "
                        f"повністю очищено. Повна копія перепсики ({len(deleted_rows)} повідомлень)."
                    ),
                )
                os.remove(fname)
            except Exception as e:
                logging.warning("Не вдалося надіслати копію чату: %s", e)

    db.commit()


# ---------------------- ФОНОВИЙ ПЛАНУВАЛЬНИК ----------------------
async def scheduler_loop(bot: Bot):
    while True:
        try:
            now_ts = time.time()
            today = datetime.now().strftime("%Y-%m-%d")

            rows = db.execute(
                "SELECT id, owner_id, chat_id, conn_id, type, payload, recurrence, weekday, hhmm "
                "FROM scheduled_tasks WHERE done=0 AND run_at<=?",
                (now_ts,),
            ).fetchall()
            for task_id, owner_id, chat_id, conn_id, ttype, payload, recurrence, weekday, hhmm in rows:
                try:
                    conn_row = db.execute(
                        "SELECT owner_chat_id FROM connections WHERE owner_id=? LIMIT 1",
                        (owner_id,),
                    ).fetchone()
                    owner_chat = conn_row[0] if conn_row else None

                    if ttype == "unmute":
                        db.execute(
                            "DELETE FROM muted WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
                        )
                        if owner_chat:
                            await bot.send_message(owner_chat, "🔊 Мут автоматично знято (час вийшов).")
                    elif ttype == "reminder" and owner_chat:
                        await bot.send_message(
                            owner_chat, f"⏰ <b>Нагадування:</b> {html.escape(payload or '')}"
                        )
                    elif ttype == "delayed_message" and conn_id and chat_id:
                        await bot.send_message(chat_id, payload, business_connection_id=conn_id)
                except Exception as e:
                    logging.warning("Помилка завдання %s: %s", task_id, e)

                if ttype == "reminder" and recurrence in ("daily", "weekly"):
                    next_run = (
                        next_weekday_occurrence(weekday, hhmm)
                        if recurrence == "weekly"
                        else next_occurrence(hhmm)
                    )
                    if next_run:
                        db.execute(
                            "UPDATE scheduled_tasks SET run_at=? WHERE id=?", (next_run, task_id)
                        )
                    else:
                        db.execute("UPDATE scheduled_tasks SET done=1 WHERE id=?", (task_id,))
                else:
                    db.execute("UPDATE scheduled_tasks SET done=1 WHERE id=?", (task_id,))
            db.commit()

            owners = db.execute("SELECT DISTINCT owner_id, owner_chat_id FROM connections").fetchall()
            for owner_id, owner_chat in owners:
                # Щоденне очищення старої історії
                last = db.execute(
                    "SELECT last_date FROM daily_tasks WHERE owner_id=? AND task='cleanup'",
                    (owner_id,),
                ).fetchone()
                if not last or last[0] != today:
                    days = get_setting_value(owner_id, "cleanup_days") or 30
                    cutoff = now_ts - days * 86400
                    db.execute(
                        "DELETE FROM messages WHERE created_at < ? AND conn_id IN "
                        "(SELECT conn_id FROM connections WHERE owner_id=?)",
                        (cutoff, owner_id),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO daily_tasks VALUES(?,?,?)",
                        (owner_id, "cleanup", today),
                    )

                # Тижневий звіт (раз на понеділок)
                if datetime.now().weekday() == 0 and get_setting(owner_id, "weekly_report_enabled"):
                    last_w = db.execute(
                        "SELECT last_date FROM daily_tasks WHERE owner_id=? AND task='weekly_report'",
                        (owner_id,),
                    ).fetchone()
                    if not last_w or last_w[0] != today:
                        since = now_ts - 7 * 86400
                        total = db.execute(
                            "SELECT COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=?",
                            (owner_id, since),
                        ).fetchone()[0]
                        top = db.execute(
                            "SELECT chat_id, COUNT(*) c FROM stats_log WHERE owner_id=? AND "
                            "ts>=? AND chat_id IS NOT NULL GROUP BY chat_id ORDER BY c DESC LIMIT 5",
                            (owner_id, since),
                        ).fetchall()
                        lines = [f"📊 <b>Тижневий звіт</b>\n\nВсього подій: {total}"]
                        if top:
                            lines.append("\nТоп активних чатів:")
                            for cid, cnt in top:
                                lines.append(
                                    f"• {html.escape(get_chat_display_name(owner_id, cid))}: {cnt}"
                                )
                        if owner_chat:
                            try:
                                await bot.send_message(owner_chat, "\n".join(lines))
                            except Exception as e:
                                logging.warning("Не вдалось надіслати тижневий звіт: %s", e)
                        db.execute(
                            "INSERT OR REPLACE INTO daily_tasks VALUES(?,?,?)",
                            (owner_id, "weekly_report", today),
                        )
            db.commit()
        except Exception as e:
            logging.warning("Помилка планувальника: %s", e)

        await asyncio.sleep(30)


# ---------------------- ЗАПУСК ----------------------
import os
from aiohttp import web

async def handle(request):
    return web.Response(text="Bot is alive")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ЗАПУСК
async def main():
    global BOT_USERNAME
    await start_web_server()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.me()
    BOT_USERNAME = me.username
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(scheduler_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
