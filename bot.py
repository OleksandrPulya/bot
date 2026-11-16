import asyncio
import aiohttp
import shutil
from collections import defaultdict, deque
import html
import logging
import os
import re
import sqlite3
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
    ChatPermissions,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-astra").strip()

logging.basicConfig(level=logging.INFO)
router = Router()
BOT_USERNAME = ""  # заповнюється під час запуску в main()
FLOOD_WINDOWS = defaultdict(deque)
SPAM_RECENT = defaultdict(deque)

# ---------------------- БАЗА ДАНИХ ----------------------
DB_PATH = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"
db = sqlite3.connect(DB_PATH)
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
CREATE TABLE IF NOT EXISTS contacts(
    owner_id INTEGER, chat_id INTEGER, user_id INTEGER, username TEXT,
    full_name TEXT, first_name TEXT, last_name TEXT, updated_at REAL,
    PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS event_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER,
    action TEXT, details TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS whitelist(
    owner_id INTEGER, chat_id INTEGER, PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS auto_rules(
    owner_id INTEGER, kind TEXT, value TEXT, enabled INTEGER DEFAULT 1,
    PRIMARY KEY(owner_id, kind));
CREATE TABLE IF NOT EXISTS tags(
    owner_id INTEGER, chat_id INTEGER, tag TEXT, PRIMARY KEY(owner_id, chat_id));
CREATE TABLE IF NOT EXISTS chat_activity(
    owner_id INTEGER, chat_id INTEGER, ts REAL, PRIMARY KEY(owner_id, chat_id));
""")
db.executescript("""
CREATE TABLE IF NOT EXISTS contact_groups(
 owner_id INTEGER, group_name TEXT, chat_id INTEGER,
 PRIMARY KEY(owner_id, group_name, chat_id));
CREATE TABLE IF NOT EXISTS deleted_archive(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, conn_id TEXT,
 chat_id INTEGER, msg_id INTEGER, from_name TEXT, text TEXT,
 media_type TEXT, file_id TEXT, from_id INTEGER, from_username TEXT,
 deleted_at REAL);
CREATE TABLE IF NOT EXISTS fraud_alerts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER,
 msg_id INTEGER, reason TEXT, content TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS backup_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, path TEXT,
 created_at REAL, size_bytes INTEGER);
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
ensure_column("settings", "weekly_report_enabled", "INTEGER DEFAULT 0")
ensure_column("scheduled_tasks", "recurrence", "TEXT")
ensure_column("scheduled_tasks", "weekday", "INTEGER")
ensure_column("scheduled_tasks", "hhmm", "TEXT")
ensure_column("stats_log", "chat_id", "INTEGER")
ensure_column("settings", "antiflood_limit", "INTEGER DEFAULT 0")
ensure_column("settings", "autotranslate_enabled", "INTEGER DEFAULT 0")
ensure_column("settings", "autotranslate_lang", "TEXT DEFAULT 'uk'")
ensure_column("settings", "autonote_keyword", "TEXT DEFAULT ''")
ensure_column("settings", "antispam_enabled", "INTEGER DEFAULT 0")
ensure_column("settings", "vip_only", "INTEGER DEFAULT 0")
ensure_column("settings", "auto_delete_hours", "INTEGER DEFAULT 0")
ensure_column("settings", "auto_backup_enabled", "INTEGER DEFAULT 0")
db.executescript("""
CREATE TABLE IF NOT EXISTS group_roles(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, level INTEGER NOT NULL,
    added_by INTEGER, created_at REAL, PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS group_command_access(
    chat_id INTEGER NOT NULL, command TEXT NOT NULL, min_level INTEGER NOT NULL,
    changed_by INTEGER, updated_at REAL, PRIMARY KEY(chat_id, command));
""")
db.commit()

SETTING_TITLES = {
    "log_edits": "Сповіщати про редагування",
    "log_deletes": "Сповіщати про видалення",
    "log_media": "Пересилати медіа при видаленні",
    "delete_cmd": "Видаляти мою команду !мут",
    "dnd_enabled": "Режим «не турбувати»",
    "autoreply_enabled": "Автовідповідь",
    "weekly_report_enabled": "📊 Тижневий звіт",
    "antispam_enabled": "🛡 Антиспам",
    "vip_only": "⭐ Сповіщення лише від VIP",
    "autotranslate_enabled": "🌐 Автоматичний переклад",
    "auto_backup_enabled": "💾 Щотижнева резервна копія",
}

DURATION_RE = re.compile(r"(\d+)\s*(хв|г|д|м|р)", re.IGNORECASE)
# 1м = 30 днів, 1р = 365 днів; фіксовані інтервали.
UNIT_SECONDS = {"хв": 60, "г": 3600, "д": 86400, "м": 30 * 86400, "р": 365 * 86400}
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
        "📖 <b>Як підключити бота до профілю</b>\n\n"
        "Підключення виконується через функцію Telegram <b>«Автоматизація чатів» (Chat Automation)</b> у налаштуваннях профілю. "
        "Відкрий чат із ботом і натисни <b>/start</b>, а потім виконай кроки нижче.\n\n"
        "<b>📱 iPhone / iOS</b>\n"
        "1. Відкрий Telegram → <b>Налаштування</b>.\n"
        "2. Знайди розділ <b>Автоматизація чатів / Chat Automation</b>.\n"
        "3. Натисни «Підключити бота» та знайди <code>@{bot_username}</code>.\n"
        "4. Вибери, до яких чатів бот матиме доступ (наприклад, нові чати або вибрані контакти).\n"
        "5. Переглянь дозволи та підтверди підключення.\n\n"
        "<b>🤖 Android</b>\n"
        "1. Відкрий Telegram → меню ☰ → <b>Налаштування</b>.\n"
        "2. Перейди до <b>Автоматизація чатів / Chat Automation</b>.\n"
        "3. Натисни «Підключити бота» та вибери <code>@{bot_username}</code>.\n"
        "4. Налаштуй список чатів, яким дозволено доступ, і підтверди підключення.\n\n"
        "<b>💻 Telegram Desktop</b>\n"
        "1. Відкрий Telegram Desktop → <b>Налаштування</b>.\n"
        "2. Знайди <b>Автоматизація чатів / Chat Automation</b> (наявність пункту залежить від версії клієнта).\n"
        "3. Вибери бота <code>@{bot_username}</code>, налаштуй доступ до чатів і підтверди.\n"
        "4. Якщо пункту немає, онови Telegram або виконай підключення в офіційному застосунку на телефоні.\n\n"
        "<b>Важливо:</b> у BotFather для бота має бути ввімкнений режим <b>Secretary Mode</b> / можливість підключення до профілю. "
        "Після успішного підключення бот отримає подію з'єднання. Саме відкриття чату з ботом або натискання /start не надає доступу до інших чатів.\n"
        "Керувати доступом або відключити бота можна в тому самому розділі <b>Автоматизація чатів</b>."
    ),
    "how": (
        "⚙️ <b>Можливості бота</b>\n\n"
        "Після підключення бота до профілю через «Автоматизацію чатів» він може обробляти повідомлення з дозволених особистих чатів:\n\n"
        "✏️ <b>Контроль редагувань:</b> надсилає старий і новий текст повідомлення; якщо воно редагувалося кілька разів, показує збережені попередні версії.\n"
        "🗑 <b>Контроль видалень:</b> зберігає копії отриманих повідомлень і повідомляє, коли вони видаляються; за ввімкненого параметра може пересилати збережені медіафайли.\n"
        "🔇 <b>Мут контактів:</b> приховує сповіщення від вибраного контакту, видаляючи його нові повідомлення через підключення до профілю; можна задати строк або вимкнути мут вручну.\n"
        "⭐ <b>VIP-контакти:</b> позначає важливих співрозмовників, чиї сповіщення проходять крізь режим «Не турбувати».\n"
        "🌙 <b>Фільтрація сповіщень:</b> тихий режим, режим лише VIP, ігнорування чатів та список дозволених контактів.\n"
        "🔑 <b>Ключові слова:</b> шукає задані слова у вхідних текстах і надсилає сповіщення; для слова можна задати чати, де воно діє або ігнорується.\n"
        "🤖 <b>Автовідповідь:</b> відповідає контакту не частіше одного разу на день; можна задати окремий текст для конкретного контакту або загальний текст і часовий інтервал.\n"
        "📝 <b>Нотатки та шаблони:</b> зберігає нотатки й шаблони відповідей, які можна вставляти командами.\n"
        "⏰ <b>Нагадування та відкладені повідомлення:</b> одноразові й повторювані нагадування, а також надсилання повідомлення пізніше.\n"
        "📊 <b>Статистика й пошук:</b> звіти, активність контактів, пошук та експорт збереженої історії.\n"
        "🛡 <b>Захист і автоматизація:</b> антиспам, антифлуд, автоматичні нотатки/мітки, автопереклад і резервні копії (за наявності налаштувань та ключів).\n"
        "🧹 <b>Очищення:</b> автоматично видаляє старі збережені повідомлення відповідно до встановленого строку.\n\n"
        "<b>Обмеження</b>\n"
        "• Бот не відновлює історію, яка існувала до підключення; він працює лише з подіями та повідомленнями, які Telegram передав після підключення.\n"
        "• Повідомлення, фото чи відео з обмеженнями перегляду можуть бути недоступні.\n"
        "• Бот не гарантує отримання кожної події: це залежить від Telegram, дозволів, доступності сервера та роботи самого бота.\n"
        "• Мут реалізований через видалення повідомлень після отримання події, тому повідомлення може короткочасно з'явитися.\n"
        "• Голосові повідомлення не перетворюються на текст; функції ШІ потребують налаштованого OPENAI_API_KEY.\n"
        "• Якщо сервер або бот вимкнений, події та заплановані дії можуть бути пропущені."
    ),
    "cmds": (
        "📌 <b>Як користуватися командами</b>\n\n"
        "Команди з ! надсилай у відповідному особистому чаті від свого імені. Команди зі / надсилай безпосередньо цьому боту.\n\n"
        "<b>🔇 Команди в особистих чатах</b>\n\n"
        "<code>!мут</code> — додати контакт до списку замучених без обмеження часу.\n"
        "<code>!мут 10хв</code> — замутити контакт на вказаний строк.\n"
        "<code>!анмут</code> — зняти мут із контакту.\n"
        "<code>!віп</code> — додати поточний контакт до VIP.\n"
        "<code>!анвіп</code> — прибрати поточний контакт із VIP.\n"
        "<code>!тількивіп</code> — увімкнути або вимкнути сповіщення лише від VIP-контактів.\n"
        "<code>!тихийрежим 2г</code> — увімкнути режим «Не турбувати» на заданий строк; без часу — до ручного вимкнення.\n"
        "<code>!повтор 10хв</code> — запланувати повторне сповіщення про цей контакт.\n"
        "<code>!цитата</code> — надіслати останнє збережене повідомлення з цього чату.\n"
        "<code>!очиститиісторію</code> — видалити локальні копії повідомлень і версій цього чату.\n"
        "<code>!автовидалення 24г</code> — встановити строк зберігання копій у годинах; <code>0г</code> вимикає цей додатковий ліміт.\n"
        "<code>!ігнор</code> — припинити обробку подій цього чату.\n"
        "<code>!деігнор</code> — знову дозволити обробку подій цього чату.\n"
        "<code>!нотатка текст</code> — зберегти вказаний текст у нотатках.\n"
        "<code>!нагадай 18:00 текст</code> — створити одноразове нагадування на найближчий вказаний час.\n"
        "<code>!нагадай щодня 18:00 текст</code> — створити щоденне нагадування.\n"
        "<code>!нагадай пн 10:00 текст</code> — створити щотижневе нагадування на вказаний день (пн, вт, ср, чт, пт, сб або нд).\n"
        "<code>!через 10хв текст</code> — надіслати в цей чат повідомлення із заданою затримкою.\n"
        "<code>!шаблон назва текст</code> — створити або оновити шаблон відповіді.\n"
        "<code>!т назва</code> — замінити команду текстом збереженого шаблону.\n"
        "<code>!ключлише слово</code> — обмежити сповіщення про це ключове слово поточним чатом.\n"
        "<code>!ключвиняток слово</code> — не надсилати сповіщення про це ключове слово з поточного чату.\n"
        "<code>!автовідповідь текст</code> — задати окрему автовідповідь для поточного контакту.\n"
        "<code>!автовідповідь off</code> — прибрати окрему автовідповідь для поточного контакту.\n"
        "<code>!дозволити</code> — додати поточний контакт до списку дозволених.\n"
        "<code>!заборонити</code> — прибрати поточний контакт зі списку дозволених.\n"
        "<code>!антиспам</code> — увімкнути або вимкнути фільтр повторюваних однакових повідомлень.\n\n"
        "<b>🤖 Команди в чаті з ботом</b>\n\n"
        "<code>/start</code> — відкрити головне меню та привітання.\n"
        "<code>/help</code> — показати довідку та меню.\n"
        "<code>/settings</code> — відкрити інтерактивні налаштування.\n"
        "<code>/stats</code> — показати кількість отриманих, відредагованих і видалених повідомлень за 7 днів.\n"
        "<code>/tasks</code> — переглянути заплановані нагадування й повідомлення та скасувати їх кнопками.\n"
        "<code>/schedule</code> — відкрити той самий список запланованих завдань.\n"
        "<code>/notes</code> — переглянути останні збережені нотатки.\n"
        "<code>/templates</code> — переглянути збережені шаблони відповідей.\n"
        "<code>/search слово [@чат] [РРРР-ММ-ДД]</code> — знайти збережені текстові повідомлення за словом, чатом та/або датою.\n"
        "<code>/export</code> — отримати текстовий файл зі збереженою історією повідомлень.\n"
        "<code>/keywords</code> — переглянути список ключових слів.\n"
        "<code>/addkeyword слово</code> — додати ключове слово до відстеження.\n"
        "<code>/delkeyword слово</code> — видалити ключове слово.\n"
        "<code>/autoreply текст</code> — увімкнути загальну автовідповідь.\n"
        "<code>/autoreply 09:00-18:00 текст</code> — надсилати загальну автовідповідь лише у вказаний проміжок часу.\n"
        "<code>/autoreply off</code> — вимкнути загальну автовідповідь.\n"
        "<code>/dnd 23:00-08:00</code> — задати години режиму «Не турбувати».\n"
        "<code>/dnd off</code> — вимкнути режим «Не турбувати».\n"
        "<code>/cleanup 30</code> — задати кількість днів зберігання історії.\n"
        "<code>/ai запит</code> — поставити запитання ШІ.\n"
        "<code>/summarize CHAT_ID</code> — отримати короткий підсумок переписки з контактом.\n"
        "<code>/reply CHAT_ID</code> — отримати три варіанти відповіді на останнє повідомлення контакту.\n"
        "<code>/translate CHAT_ID [мова]</code> — перекласти збережену переписку.\n"
        "<code>/explain CHAT_ID</code> — отримати пояснення останнього повідомлення.\n"
        "<code>/sentiment CHAT_ID</code> — отримати нейтральний аналіз тону переписки.\n"
        "<code>/smartsearch запит</code> — знайти повідомлення за змістом, а не лише за точним словом.\n"
        "<code>/tag CHAT_ID мітка</code> — призначити контакту мітку.\n"
        "<code>/tags</code> — переглянути мітки контактів.\n"
        "<code>/top</code> — показати найактивніші контакти за 7 днів.\n"
        "<code>/activity</code> — показати кількість подій за кожен день останнього тижня.\n"
        "<code>/inactive</code> — показати контакти без активності понад 7 днів.\n"
        "<code>/chatstats CHAT_ID</code> — показати статистику конкретного контакту.\n"
        "<code>/deleted</code> — показати кількість видалень за 7 днів.\n"
        "<code>/edited</code> — показати кількість редагувань за 7 днів.\n"
        "<code>/report</code> — сформувати звіт за 7 днів і топ контактів.\n"
        "<code>/security</code> — перевірити наявність основних ключів і стан окремих параметрів безпеки.\n"
        "<code>/logs</code> — переглянути останні записи журналу подій.\n"
        "<code>/backup</code> — створити й отримати резервну копію бази даних.\n"
        "<code>/backup auto</code> — увімкнути щотижневу резервну копію.\n"
        "<code>/backup off</code> — вимкнути автоматичне резервне копіювання.\n"
        "<code>/restore</code> — відновити базу з файлу .db, відповівши цією командою на повідомлення з файлом.\n"
        "<code>/whitelist</code> — переглянути список дозволених контактів.\n"
        "<code>/antiflood 20</code> — встановити ліміт 20 повідомлень за хвилину; <code>/antiflood 0</code> вимикає ліміт.\n"
        "<code>/antispam</code> — перемкнути фільтр повторюваних повідомлень.\n"
        "<code>/viponly</code> — перемкнути режим сповіщень лише від VIP.\n"
        "<code>/autotranslate on uk</code> — увімкнути автоматичний переклад українською (можна вказати іншу мову).\n"
        "<code>/autotranslate off</code> — вимкнути автопереклад.\n"
        "<code>/autonote слово</code> — автоматично зберігати повідомлення, що містять слово; <code>/autonote off</code> вимикає функцію.\n"
        "<code>/autotag слово мітка</code> — налаштувати автоматичне призначення мітки за словом; <code>/autotag off</code> вимикає.\n"
        "<code>/blocked</code> — переглянути список замучених контактів.\n"
        "<code>/daily</code> — показати підсумок подій за останні 24 години.\n"
        "<code>/rules</code> — переглянути активні автоматичні правила.\n"
        "<code>/history CHAT_ID [MESSAGE_ID]</code> — переглянути історію редагувань.\n"
        "<code>/group</code> — керувати групами контактів.\n"
        "<code>/contact CHAT_ID</code> — відкрити картку контакту.\n"
        "<code>/dashboard</code> — відкрити панель керування.\n"
        "<code>/archive</code> — переглянути архів видалених повідомлень.\n"
        "<code>/smartremind HH:MM текст</code> — створити розумне нагадування.\n"
        "<code>/fraud</code> — переглянути попередження про шахрайство.\n"
        "<code>/backups</code> — переглянути історію резервних копій."
    ),
    "privacy": (
        "🔒 <b>Приватність і дані</b>\n\n"
        "<b>Які дані зберігаються</b>\n"
        "• Дані підключення бота до профілю: ідентифікатор підключення, ID власника та ID чату для надсилання сповіщень.\n"
        "• Повідомлення з підключених чатів: текст або підпис, тип медіа, Telegram file_id, дата/час, ID та ім'я відправника, username, ID чату й повідомлення.\n"
        "• Попередні версії відредагованих повідомлень — щоб можна було показати історію змін.\n"
        "• Контакти та назви чатів, мітки, список VIP, замучені/ігноровані/дозволені чати.\n"
        "• Налаштування бота: режими сповіщень, DND, автовідповідь, строки зберігання, антиспам, антифлуд та інші параметри.\n"
        "• Ключові слова й правила їх застосування, нотатки, шаблони, нагадування, відкладені завдання та журнали подій.\n"
        "• Статистика активності й подій, а також дати останнього автоматичного очищення/резервного копіювання.\n\n"
        "<b>Де і як обробляються дані</b>\n"
        "• Дані зберігаються у файлі SQLite <code>bot.db</code> на сервері, де запущений бот. У цьому коді база не шифрується; безпека залежить від захисту сервера, резервних копій і доступу до файлів.\n"
        "• Бот надсилає власнику сповіщення та копії подій у приватний чат із ботом. Експорт і резервні копії надсилаються лише на запит або за ввімкненого автоматичного резервування.\n"
        "• Якщо використовуються <code>/ai</code>, <code>/summarize</code>, <code>/reply</code>, <code>/translate</code>, <code>/explain</code>, <code>/sentiment</code> або семантичний пошук, вибраний текст переписки передається до OpenAI API. Автопереклад також передає текст зовнішньому сервісу, якщо ця функція реалізована та ввімкнена у розгорнутій версії.\n"
        "• Telegram обробляє повідомлення та підключення бота до профілю відповідно до власної політики приватності й налаштувань сервісу.\n\n"
        "<b>Що бот не робить</b>\n"
        "• Не має доступу до всієї старої історії до моменту підключення.\n"
        "• Не отримує автоматично пароль Telegram або код входу.\n"
        "• Сам файл бази даних не публікується, але адміністратор сервера, де працює бот, технічно може мати доступ до нього.\n\n"
        "<b>Керування та видалення</b>\n"
        "• Вимкни доступ у Telegram: <b>Налаштування → Автоматизація чатів / Chat Automation</b> та видали/відключи цього бота. Це припинить надходження нових подій, але не видалить уже збережену базу.\n"
        "• Команда <code>!очиститиісторію</code> видаляє локальну історію конкретного чату.\n"
        "• Кнопка «Видалити збережені копії» у цьому розділі видаляє з бази повідомлення та їхні версії для всіх підключених чатів; інші дані (нотатки, налаштування, журнали, контакти, завдання тощо) вона не видаляє.\n"
        "• <code>/cleanup</code> задає строк зберігання історії; автоматичне очищення видаляє старі повідомлення та версії, але не всі інші категорії даних.\n"
        "• Резервні копії є окремими файлами. Видалення даних із поточної бази не стирає копії, які вже були завантажені або збережені окремо.\n\n"
        "<b>Відповідальне використання</b>\n"
        "Повідомлення можуть містити особисту або конфіденційну інформацію. Захищай доступ до бота й сервера, не передавай токени та ключі API, не надсилай чутливі тексти до ШІ без потреби та повідомляй співрозмовників про збереження/обробку їхніх повідомлень, коли цього вимагають закон і правила платформи.\n\n"
        "<i>Цей опис відображає логіку наданого коду. Фактична обробка залежить від конфігурації хостингу, підключених сервісів і версії Telegram.</i>"
    ),
}

WELCOME = (
    "👋 <b>Привіт! Я — персональний Telegram-бот для контролю та автоматизації дозволених особистих чатів.</b>\n\n"
    "Я можу повідомляти про редагування й видалення повідомлень, зберігати їхні копії, "
    "відстежувати ключові слова, керувати сповіщеннями та VIP-контактами, застосовувати мут, "
    "створювати нагадування й відкладені повідомлення, вести нотатки та шаблони, "
    "формувати статистику й звіти, експортувати історію, а також використовувати функції ШІ, "
    "перекладу, антиспаму й автоматизації — якщо відповідні модулі налаштовані.\n\n"
    "Для початку відкрий «Як підключити» та підключи його в розділі «Автоматизація чатів / Chat Automation». "
    "У розділі «Команди» є повний перелік команд і пояснення.\n\n"
    "⚠️ Бот зберігає копії повідомлень на сервері. Ознайомся з розділом «Приватність» перед підключенням."
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


async def resolve_business_connection(bot: Bot, conn_id: str):
    """Recover the owner mapping from Telegram if the local SQLite row was lost."""
    conn = get_conn(conn_id)
    if conn:
        return conn
    try:
        business = await bot.get_business_connection(conn_id)
        if not business or not business.is_enabled:
            logging.warning("Business connection %s is missing or disabled", conn_id)
            return None
        owner_id = business.user.id
        owner_chat_id = business.user_chat_id
        db.execute(
            "INSERT OR REPLACE INTO connections(conn_id, owner_id, owner_chat_id) VALUES(?,?,?)",
            (conn_id, owner_id, owner_chat_id),
        )
        db.commit()
        logging.info("Recovered business connection %s for owner %s", conn_id, owner_id)
        return owner_id, owner_chat_id
    except Exception:
        logging.exception("Could not recover business connection %s from Telegram", conn_id)
        return None


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
    """Перевірка VIP-only та DND. VIP-контакти проходять крізь DND."""
    if get_setting(owner_id, "vip_only") and not is_vip(owner_id, chat_id):
        return False
    return is_vip(owner_id, chat_id) or not in_dnd(owner_id)


def get_chat_display_name(owner_id: int, chat_id: int) -> str:
    row = db.execute(
        "SELECT name FROM chat_names WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
    ).fetchone()
    return row[0] if row and row[0] else str(chat_id)


def format_sender(name: str, user_id, username: str = None) -> str:
    safe_name = html.escape(name or "Невідомо")
    safe_id = html.escape(str(user_id))
    if username:
        safe_username = html.escape(str(username).lstrip("@"), quote=True)
        profile_url = f"https://t.me/{safe_username}"
        name_link = f'<a href="{profile_url}">{safe_name}</a>'
        nick_link = f'<a href="{profile_url}">@{html.escape(str(username).lstrip("@"))}</a>'
    elif str(user_id).isdigit():
        profile_url = f"tg://user?id={int(user_id)}"
        name_link = f'<a href="{profile_url}">{safe_name}</a>'
        nick_link = name_link
    else:
        name_link = safe_name
        nick_link = "нікнейм недоступний"
    return f"{name_link} ({nick_link}) [<code>{safe_id}</code>]"


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


# ---------------------- ЄДИНА ІДЕНТИФІКАЦІЯ КОНТАКТІВ ----------------------
def remember_contact(owner_id: int, m: Message):
    user = m.from_user
    if not user or user.id == owner_id:
        return
    db.execute(
        "INSERT OR REPLACE INTO contacts(owner_id,chat_id,user_id,username,full_name,first_name,last_name,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (owner_id, m.chat.id, user.id, user.username, user.full_name,
         user.first_name, user.last_name, time.time()),
    )
    db.execute("INSERT OR REPLACE INTO chat_names(owner_id,chat_id,name) VALUES(?,?,?)",
               (owner_id, m.chat.id, m.chat.full_name or user.full_name or str(m.chat.id)))
    db.execute("INSERT OR REPLACE INTO chat_activity(owner_id,chat_id,ts) VALUES(?,?,?)",
               (owner_id, m.chat.id, time.time()))
    db.commit()


def contact_card(owner_id: int, chat_id: int, action: str, details: str = "") -> str:
    row = db.execute(
        "SELECT user_id,username,full_name FROM contacts WHERE owner_id=? AND chat_id=?",
        (owner_id, chat_id),
    ).fetchone()
    if not row:
        # Backfill identity from messages saved by earlier bot versions.
        row = db.execute(
            "SELECT from_id,from_username,from_name FROM messages "
            "WHERE chat_id=? AND conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?) "
            "AND from_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (chat_id, owner_id),
        ).fetchone()
        if row:
            db.execute("INSERT OR REPLACE INTO contacts(owner_id,chat_id,user_id,username,full_name,updated_at) VALUES(?,?,?,?,?,?)",
                       (owner_id,chat_id,row[0],row[1],row[2],time.time()))
            db.commit()
    if row:
        user_id, username, full_name = row
    else:
        user_id, username = None, None
        full_name = get_chat_display_name(owner_id, chat_id)
    name = html.escape(full_name or "Невідомо")
    uid = f"<code>{user_id}</code>" if user_id else "Невідомий (Telegram не передав ID користувача)"

    # Посилання розміщене безпосередньо на імені контакту.
    # Якщо Telegram передав ID — відкриваємо профіль через tg://user?id=...
    # Якщо ID немає, але є username — використовуємо посилання t.me.
    if user_id:
        clickable_name = f'<a href="tg://user?id={int(user_id)}"><b>{name}</b></a>'
    elif username:
        safe_username = html.escape(str(username).lstrip("@"), quote=True)
        clickable_name = f'<a href="https://t.me/{safe_username}"><b>{name}</b></a>'
    else:
        clickable_name = f"<b>{name}</b>"

    # Показуємо саме контакт, ДО ЯКОГО застосували команду.
    lines = [
        "🎯 <b>Команду виконано для контакту:</b>",
        f"{clickable_name} [ {uid} ]",
        f"<b>Дія:</b> {html.escape(action)}",
        f"<b>Chat ID:</b> <code>{chat_id}</code>",
    ]
    if details:
        lines.append(f"<b>Деталі:</b> {html.escape(details)}")
    return "\n".join(lines)


async def notify_contact(bot: Bot, owner_chat: int, owner_id: int, chat_id: int,
                         action: str, details: str = ""):
    db.execute("INSERT INTO event_logs(owner_id,chat_id,action,details,ts) VALUES(?,?,?,?,?)",
               (owner_id, chat_id, action, details, time.time()))
    db.commit()
    # Додаємо тег саме цього бота в кінці сповіщення, відокремивши його двома переносами.
    bot_tag = f"@{BOT_USERNAME}" if BOT_USERNAME else ""
    message_text = contact_card(owner_id, chat_id, action, details)
    if bot_tag:
        message_text += f"\n\n{html.escape(bot_tag)}"
    await bot.send_message(owner_chat, message_text)


def log_event(owner_id: int, chat_id: int | None, action: str, details: str = ""):
    db.execute("INSERT INTO event_logs(owner_id,chat_id,action,details,ts) VALUES(?,?,?,?,?)",
               (owner_id, chat_id, action, details, time.time()))
    db.commit()


async def openai_text(prompt: str, system: str = "Ти корисний асистент. Відповідай українською.") -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("Не задано OPENAI_API_KEY у змінних середовища.")
    payload = {"model": OPENAI_MODEL,
               "input": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
               "max_output_tokens": 900}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.openai.com/v1/responses", json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
                raise RuntimeError(msg)
    if data.get("output_text"):
        return data["output_text"].strip()
    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    return "\n".join(parts).strip() or "Модель не повернула текстову відповідь."


async def ai_command(m: Message, mode: str):
    parts = (m.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    owner_id = m.from_user.id
    chat_id = None
    target_arg = arg
    translate_lang = "українська"
    if mode == "translate" and arg:
        chunks = arg.split()
        target_arg = chunks[0]
        if len(chunks) > 1:
            translate_lang = chunks[1]
    if mode in ("summarize", "reply", "translate", "explain", "sentiment"):
        lookup = target_arg[1:] if target_arg.startswith("@") else target_arg
        rows = db.execute(
            "SELECT chat_id FROM chat_names WHERE owner_id=? AND name LIKE ? ORDER BY chat_id LIMIT 1",
            (owner_id, f"%{lookup}%"),
        ).fetchall() if lookup and not lookup.isdigit() else []
        if rows:
            chat_id = rows[0][0]
        elif lookup.isdigit():
            chat_id = int(lookup)
    if mode == "ai":
        if not arg:
            await m.answer("Використання: <code>/ai твоє запитання</code>")
            return
        prompt = arg
        system = ("Ти розширений персональний асистент Telegram. Відповідай українською, "
                  "структуровано й практично. Допомагай складати повідомлення в різних стилях, "
                  "плани, списки дій, пояснення та підсумки. Не вигадуй факти; за нестачі даних уточнюй.")
    else:
        if not chat_id:
            await m.answer("Вкажи ID або частину назви контакту. Наприклад: <code>/summarize 123456789</code>")
            return
        rows = db.execute(
            "SELECT from_name,text,created_at FROM messages WHERE chat_id=? AND conn_id IN "
            "(SELECT conn_id FROM connections WHERE owner_id=?) ORDER BY created_at DESC LIMIT 80",
            (chat_id, owner_id),
        ).fetchall()
        rows.reverse()
        transcript = "\n".join(f"{n}: {t or '[медіа]'}" for n,t,_ in rows if t or n)
        if not transcript:
            await m.answer("Для цього контакту ще немає збереженої історії.")
            return
        prompts = {
            "summarize": "Стисло підсумуй переписку, виділи домовленості, питання й важливі факти.",
            "reply": "Запропонуй 3 природні варіанти відповіді на останнє повідомлення співрозмовника.",
            "translate": f"Переклади переписку мовою {translate_lang}. Не додавай пояснень.",
            "explain": "Поясни простими словами зміст останнього повідомлення та можливий підтекст, не вигадуй факти.",
            "sentiment": "Проаналізуй тон спілкування нейтрально, наведи ознаки, не став діагнозів і не роби категоричних висновків про людину.",
        }
        system = "Аналізуй лише наданий текст. Не домислюй відсутні факти. Відповідай українською."
        prompt = prompts[mode] + "\n\nІСТОРІЯ:\n" + transcript[-18000:]
    try:
        answer = await openai_text(prompt, system)
        await m.answer("🤖 <b>Відповідь ШІ</b>\n\n" + html.escape(answer[:4000]))
    except Exception as e:
        logging.exception("OpenAI request failed")
        await m.answer("⚠️ Не вдалося отримати відповідь ШІ: " + html.escape(str(e)[:500]))


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
    kb.button(text="📊 Статистика")
    kb.button(text="📝 Нотатки")
    kb.button(text="📋 Завдання")
    kb.adjust(2, 2)
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
    kb.button(text="⏱ Налаштувати антифлуд", callback_data="setting:antiflood")
    kb.button(text="🕒 Налаштувати строк зберігання", callback_data="setting:cleanup")
    kb.button(text="🌍 Мова автоперекладу", callback_data="setting:translate_lang")
    kb.button(text="« Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def muted_keyboard(owner_id: int):
    rows = db.execute(
        "SELECT chat_id, chat_name, until FROM muted WHERE owner_id=?", (owner_id,)
    ).fetchall()
    kb = InlineKeyboardBuilder()
    if rows:
        text = "🔇 <b>Замучені контакти</b>\n\nНатисни контакт, щоб зняти мут."
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
        text = "VIP-контактів немає. Додай командою <code>!віп</code> у чаті з людиною."
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


@router.message(Command("settings"))
async def cmd_settings(m: Message):
    await m.answer(
        "🛠 <b>Налаштування</b>\n\nНатискай кнопки, щоб змінити параметри. Для значень, які потребують введення тексту або часу, скористайся відповідною командою з розділу «Команди».",
        reply_markup=settings_keyboard(m.from_user.id),
    )


def stats_contact_link(owner_id: int, chat_id: int):
    """Return a Telegram deep link and a display name for a managed private chat."""
    row = db.execute(
        "SELECT user_id, username, full_name FROM contacts WHERE owner_id=? AND chat_id=?",
        (owner_id, chat_id),
    ).fetchone()
    user_id = row[0] if row else None
    username = row[1] if row else None
    full_name = row[2] if row else None
    if not full_name:
        full_name = get_chat_display_name(owner_id, chat_id)
    if not user_id and chat_id > 0:
        # In a private Telegram dialog, chat_id is the peer's user ID.
        user_id = chat_id
    if username:
        url = "https://t.me/" + str(username).lstrip("@")
    elif user_id:
        url = f"tg://user?id={int(user_id)}"
    else:
        url = None
    return full_name or str(chat_id), user_id, username, url


def stats_top_lines(owner_id: int, event: str, limit: int = 10) -> str:
    rows = db.execute(
        "SELECT chat_id, COUNT(*) AS total FROM stats_log "
        "WHERE owner_id=? AND event=? AND chat_id IS NOT NULL "
        "GROUP BY chat_id ORDER BY total DESC, chat_id ASC LIMIT ?",
        (owner_id, event, limit),
    ).fetchall()
    if not rows:
        return "Поки немає даних."
    lines = []
    for rank, (chat_id, total) in enumerate(rows, 1):
        name, user_id, username, url = stats_contact_link(owner_id, chat_id)
        safe_name = html.escape(str(name))
        if url:
            clickable = f'<a href="{html.escape(url, quote=True)}">{safe_name}</a>'
            id_text = f'<a href="{html.escape(url, quote=True)}"><code>{user_id or chat_id}</code></a>'
        else:
            clickable = safe_name
            id_text = f"<code>{chat_id}</code>"
        if username:
            safe_username = html.escape(str(username).lstrip("@"), quote=True)
            nick = f'<a href="https://t.me/{safe_username}">@{html.escape(str(username).lstrip("@"))}</a>'
        elif user_id:
            nick = f'<a href="tg://user?id={int(user_id)}">відкрити чат</a>'
        else:
            nick = "нікнейм відсутній"
        lines.append(f"{rank}. {clickable} ({nick}) — <b>{total}</b> повідомлень · ID: {id_text}")
    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    owner_id = m.from_user.id
    rows = db.execute(
        "SELECT event, COUNT(*) FROM stats_log WHERE owner_id=? GROUP BY event",
        (owner_id,),
    ).fetchall()
    counts = {"received": 0, "edited": 0, "deleted": 0}
    counts.update(dict(rows))

    text = (
        "📊 <b>Загальна статистика</b>\n\n"
        f"📨 Отримано повідомлень: <b>{counts['received']}</b>\n"
        f"✏️ Відредаговано повідомлень: <b>{counts['edited']}</b>\n"
        f"🗑 Видалено повідомлень: <b>{counts['deleted']}</b>\n\n"
        "<b>🏆 ТОП чатів за отриманими повідомленнями</b>\n"
        f"{stats_top_lines(owner_id, 'received')}\n\n"
        "<b>🗑 ТОП чатів за видаленими повідомленнями</b>\n"
        f"{stats_top_lines(owner_id, 'deleted')}\n\n"
        "<b>✏️ ТОП чатів за відредагованими повідомленнями</b>\n"
        f"{stats_top_lines(owner_id, 'edited')}"
    )
    # Telegram message limit is 4096 characters; split only between lines/sections.
    if len(text) <= 4000:
        await m.answer(text, disable_web_page_preview=True)
    else:
        sections = text.split("\n\n")
        chunk = ""
        for section in sections:
            candidate = (chunk + "\n\n" + section).strip()
            if len(candidate) > 3900 and chunk:
                await m.answer(chunk, disable_web_page_preview=True)
                chunk = section
            else:
                chunk = candidate
        if chunk:
            await m.answer(chunk, disable_web_page_preview=True)


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


# ---------------------- НОВІ КОМАНДИ: ШІ, АНАЛІТИКА, БЕЗПЕКА ----------------------
@router.message(Command("ai"))
async def cmd_ai(m: Message): await ai_command(m, "ai")
@router.message(Command("summarize"))
async def cmd_summarize(m: Message): await ai_command(m, "summarize")
@router.message(Command("reply"))
async def cmd_reply(m: Message): await ai_command(m, "reply")
@router.message(Command("translate"))
async def cmd_translate(m: Message): await ai_command(m, "translate")
@router.message(Command("explain"))
async def cmd_explain(m: Message): await ai_command(m, "explain")
@router.message(Command("sentiment"))
async def cmd_sentiment(m: Message): await ai_command(m, "sentiment")

@router.message(Command("smartsearch"))
async def cmd_smartsearch(m: Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2:
        await m.answer("Формат: <code>/smartsearch про що домовлялися щодо зустрічі</code>"); return
    owner_id=m.from_user.id
    rows=db.execute("SELECT from_name,text,chat_id,created_at FROM messages WHERE conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?) AND text IS NOT NULL ORDER BY created_at DESC LIMIT 100",(owner_id,)).fetchall()
    if not rows:
        await m.answer("Немає збережених текстових повідомлень для пошуку."); return
    corpus="\n".join(f"[{datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')}] chat={cid} {name}: {txt}" for name,txt,cid,ts in rows)
    try:
        answer=await openai_text("Знайди повідомлення, які за змістом відповідають запиту. Поверни короткий список з датою, Chat ID, автором і фрагментом. Якщо збігів немає — так і скажи. Не вигадуй.\nЗАПИТ: "+parts[1]+"\nКАНДИДАТИ:\n"+corpus[-20000:],"Ти виконуєш пошук по наданому корпусу повідомлень. Використовуй лише наведені дані.")
        await m.answer("🧠 <b>Семантичний пошук</b>\n\n"+html.escape(answer[:4000]))
    except Exception as e:
        await m.answer("⚠️ Помилка пошуку: "+html.escape(str(e)[:400]))

@router.message(Command("tag"))
async def cmd_tag(m: Message):
    parts=(m.text or "").split(maxsplit=2)
    if len(parts)<3: await m.answer("Формат: <code>/tag CHAT_ID мітка</code>"); return
    try: cid=int(parts[1])
    except ValueError: await m.answer("Chat ID має бути числом."); return
    db.execute("INSERT OR REPLACE INTO tags(owner_id,chat_id,tag) VALUES(?,?,?)",(m.from_user.id,cid,parts[2])); db.commit()
    await m.answer(f"🏷 Контакту {cid} присвоєно мітку «{html.escape(parts[2])}».")

@router.message(Command("top"))
async def cmd_top(m: Message):
    since=time.time()-7*86400
    rows=db.execute("SELECT chat_id,COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=? AND chat_id IS NOT NULL GROUP BY chat_id ORDER BY COUNT(*) DESC LIMIT 10",(m.from_user.id,since)).fetchall()
    await m.answer("🏆 <b>Найактивніші контакти за 7 днів</b>\n\n"+("\n".join(f"• {html.escape(get_chat_display_name(m.from_user.id,cid))} — {n}" for cid,n in rows) if rows else "Поки немає даних."))

@router.message(Command("activity"))
async def cmd_activity(m: Message):
    since=time.time()-7*86400
    rows=db.execute("SELECT date(ts,'unixepoch','localtime'),COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=? GROUP BY 1 ORDER BY 1",(m.from_user.id,since)).fetchall()
    await m.answer("📈 <b>Активність за 7 днів</b>\n\n"+("\n".join(f"{d}: {n} подій" for d,n in rows) if rows else "Даних поки немає."))

@router.message(Command("inactive"))
async def cmd_inactive(m: Message):
    cutoff=time.time()-7*86400
    rows=db.execute("SELECT c.chat_id,c.name,a.ts FROM chat_names c LEFT JOIN chat_activity a ON a.owner_id=c.owner_id AND a.chat_id=c.chat_id WHERE c.owner_id=? AND (a.ts IS NULL OR a.ts<?) ORDER BY a.ts LIMIT 30",(m.from_user.id,cutoff)).fetchall()
    await m.answer("💤 <b>Контакти без активності понад 7 днів</b>\n\n"+("\n".join(f"• {html.escape(n or str(cid))} — {('ніколи' if ts is None else datetime.fromtimestamp(ts).strftime('%d.%m.%Y'))}" for cid,n,ts in rows) if rows else "Таких контактів не знайдено."))

@router.message(Command("chatstats"))
async def cmd_chatstats(m: Message):
    parts=m.text.split(maxsplit=1)
    if len(parts)<2: await m.answer("Формат: <code>/chatstats CHAT_ID</code>"); return
    try: cid=int(parts[1])
    except ValueError: await m.answer("Потрібен числовий Chat ID."); return
    rows=db.execute("SELECT event,COUNT(*) FROM stats_log WHERE owner_id=? AND chat_id=? AND ts>=? GROUP BY event",(m.from_user.id,cid,time.time()-7*86400)).fetchall()
    await m.answer(contact_card(m.from_user.id,cid,"Статистика контакту")+"\n\n"+("\n".join(f"{e}: {n}" for e,n in rows) if rows else "Подій за 7 днів немає."))

@router.message(Command("deleted"))
async def cmd_deleted(m: Message):
    since=time.time()-7*86400
    n=db.execute("SELECT COUNT(*) FROM stats_log WHERE owner_id=? AND event='deleted' AND ts>=?",(m.from_user.id,since)).fetchone()[0]
    await m.answer(f"🗑 Видалень за 7 днів: <b>{n}</b>")

@router.message(Command("edited"))
async def cmd_edited(m: Message):
    since=time.time()-7*86400
    n=db.execute("SELECT COUNT(*) FROM stats_log WHERE owner_id=? AND event='edited' AND ts>=?",(m.from_user.id,since)).fetchone()[0]
    await m.answer(f"✏️ Редагувань за 7 днів: <b>{n}</b>")

@router.message(Command("report"))
async def cmd_report(m: Message):
    since=time.time()-7*86400
    rows=db.execute("SELECT event,COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=? GROUP BY event",(m.from_user.id,since)).fetchall()
    top=db.execute("SELECT chat_id,COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=? AND chat_id IS NOT NULL GROUP BY chat_id ORDER BY COUNT(*) DESC LIMIT 5",(m.from_user.id,since)).fetchall()
    body="\n".join(f"• {e}: {n}" for e,n in rows) or "Подій немає."
    body += "\n\n<b>Топ контактів:</b>\n" + ("\n".join(f"• {html.escape(get_chat_display_name(m.from_user.id,c))}: {n}" for c,n in top) or "Немає даних")
    await m.answer("📊 <b>Звіт за 7 днів</b>\n\n"+body)

@router.message(Command("security"))
async def cmd_security(m: Message):
    await m.answer("🔐 <b>Перевірка безпеки</b>\n\n"+
        f"BOT_TOKEN: {'налаштований' if TOKEN else 'НЕ налаштований'}\n"+
        f"OPENAI_API_KEY: {'налаштований' if OPENAI_API_KEY else 'не налаштований'}\n"+
        f"Режим DND: {'увімкнений' if get_setting(m.from_user.id,'dnd_enabled') else 'вимкнений'}\n"+
        f"Антиспам: {'увімкнений' if get_setting(m.from_user.id,'antispam_enabled') else 'вимкнений'}\n"+
        "Рекомендація: не надсилай токени або ключі в чатах і зберігай їх лише у змінних середовища.")

@router.message(Command("logs"))
async def cmd_logs(m: Message):
    rows=db.execute("SELECT action,details,ts,chat_id FROM event_logs WHERE owner_id=? ORDER BY ts DESC LIMIT 20",(m.from_user.id,)).fetchall()
    await m.answer("📜 <b>Останні події</b>\n\n"+("\n".join(f"{datetime.fromtimestamp(ts).strftime('%d.%m %H:%M')} — {html.escape(a)} — {html.escape(str(cid))} {html.escape(d or '')}" for a,d,ts,cid in rows) if rows else "Журнал поки порожній."))

@router.message(Command("backup"))
async def cmd_backup(m: Message):
    owner_id=m.from_user.id
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)>1 and parts[1].strip().lower() in ("auto","off"):
        enabled=parts[1].strip().lower()=="auto"
        get_setting(owner_id,"auto_backup_enabled")
        db.execute("UPDATE settings SET auto_backup_enabled=? WHERE owner_id=?",(int(enabled),owner_id)); db.commit()
        await m.answer("🛡 Щотижневе резервне копіювання увімкнено. Копія надсилатиметься в понеділок." if enabled else "Автоматичне резервне копіювання вимкнено.")
        return
    os.makedirs("backups", exist_ok=True)
    path=os.path.join("backups", f"backup_{owner_id}_{int(time.time())}.db")
    target=sqlite3.connect(path)
    try: db.commit(); db.backup(target)
    finally: target.close()
    size_bytes = os.path.getsize(path)
    db.execute("INSERT INTO backup_history(owner_id,path,created_at,size_bytes) VALUES(?,?,?,?)",
               (owner_id, path, time.time(), size_bytes)); db.commit()
    # Залишаємо останні 5 записів історії на сервері; файл, який надсилаємо зараз, видаляємо після відправлення.
    await m.answer_document(FSInputFile(path),caption=f"🛡 Резервна копія SQLite\nРозмір: {size_bytes:,} байт\nІсторія копій: /backups")
    old = db.execute("SELECT id,path FROM backup_history WHERE owner_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 5", (owner_id,)).fetchall()
    for old_id, old_path in old:
        try:
            if old_path and os.path.exists(old_path): os.remove(old_path)
        except OSError: pass
        db.execute("DELETE FROM backup_history WHERE id=?", (old_id,))
    db.commit()

@router.message(Command("restore"))
async def cmd_restore(m: Message):
    if not m.reply_to_message or not m.reply_to_message.document:
        await m.answer("Відповідай командою /restore на повідомлення з резервною копією .db.")
        return
    doc=m.reply_to_message.document
    if not (doc.file_name or "").lower().endswith((".db",".sqlite",".sqlite3")):
        await m.answer("Потрібен файл резервної копії SQLite (.db/.sqlite/.sqlite3).")
        return
    info=await m.bot.get_file(doc.file_id)
    temp=f"restore_upload_{m.from_user.id}.db"
    await m.bot.download_file(info.file_path,temp)
    backup=f"before_restore_{m.from_user.id}_{int(time.time())}.db"
    try:
        test=sqlite3.connect(temp)
        check=test.execute("PRAGMA integrity_check").fetchone()[0]
        tables={r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if check!="ok" or "connections" not in tables or "messages" not in tables:
            test.close()
            await m.answer("Файл не пройшов перевірку цілісності або не схожий на базу цього бота.")
            return
        db.commit()
        current=sqlite3.connect("bot.db")
        try: db.backup(current)
        finally: current.close()
        try:
            test.backup(db)
            db.commit()
            # Міграції для резервних копій старішої версії.
            db.executescript("""
            CREATE TABLE IF NOT EXISTS contacts(owner_id INTEGER,chat_id INTEGER,user_id INTEGER,username TEXT,full_name TEXT,first_name TEXT,last_name TEXT,updated_at REAL,PRIMARY KEY(owner_id,chat_id));
            CREATE TABLE IF NOT EXISTS event_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,chat_id INTEGER,action TEXT,details TEXT,ts REAL);
            CREATE TABLE IF NOT EXISTS whitelist(owner_id INTEGER,chat_id INTEGER,PRIMARY KEY(owner_id,chat_id));
            CREATE TABLE IF NOT EXISTS auto_rules(owner_id INTEGER,kind TEXT,value TEXT,enabled INTEGER DEFAULT 1,PRIMARY KEY(owner_id,kind));
            CREATE TABLE IF NOT EXISTS tags(owner_id INTEGER,chat_id INTEGER,tag TEXT,PRIMARY KEY(owner_id,chat_id));
            CREATE TABLE IF NOT EXISTS chat_activity(owner_id INTEGER,chat_id INTEGER,ts REAL,PRIMARY KEY(owner_id,chat_id));
            CREATE TABLE IF NOT EXISTS contact_groups(owner_id INTEGER,group_name TEXT,chat_id INTEGER,PRIMARY KEY(owner_id,group_name,chat_id));
            CREATE TABLE IF NOT EXISTS deleted_archive(id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,conn_id TEXT,chat_id INTEGER,msg_id INTEGER,from_name TEXT,text TEXT,media_type TEXT,file_id TEXT,from_id INTEGER,from_username TEXT,deleted_at REAL);
            CREATE TABLE IF NOT EXISTS fraud_alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,chat_id INTEGER,msg_id INTEGER,reason TEXT,content TEXT,created_at REAL);
            CREATE TABLE IF NOT EXISTS backup_history(id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,path TEXT,created_at REAL,size_bytes INTEGER);
            """)
            for table,col,decl in [("settings","antiflood_limit","INTEGER DEFAULT 0"),("settings","autotranslate_enabled","INTEGER DEFAULT 0"),("settings","autotranslate_lang","TEXT DEFAULT 'uk'"),("settings","autonote_keyword","TEXT DEFAULT ''"),("settings","antispam_enabled","INTEGER DEFAULT 0"),("settings","vip_only","INTEGER DEFAULT 0"),("settings","auto_delete_hours","INTEGER DEFAULT 0"),("settings","auto_backup_enabled","INTEGER DEFAULT 0"),("settings","dnd_enabled","INTEGER DEFAULT 0"),("settings","dnd_start","TEXT DEFAULT '23:00'"),("settings","dnd_end","TEXT DEFAULT '08:00'"),("settings","autoreply_enabled","INTEGER DEFAULT 0"),("settings","autoreply_text","TEXT DEFAULT ''"),("settings","autoreply_start","TEXT DEFAULT ''"),("settings","autoreply_end","TEXT DEFAULT ''"),("settings","cleanup_days","INTEGER DEFAULT 30"),("settings","weekly_report_enabled","INTEGER DEFAULT 0"),("muted","until","REAL"),("muted","chat_name","TEXT"),("scheduled_tasks","recurrence","TEXT"),("scheduled_tasks","weekday","INTEGER"),("scheduled_tasks","hhmm","TEXT"),("messages","from_id","INTEGER"),("messages","from_username","TEXT"),("messages","created_at","REAL DEFAULT 0"),("stats_log","chat_id","INTEGER")]:
                ensure_column(table,col,decl)
            db.commit()
        except Exception:
            rollback=sqlite3.connect(backup)
            try: rollback.backup(db); db.commit()
            finally: rollback.close()
            raise
        test.close()
        await m.answer("✅ Базу відновлено. Перед відновленням поточну базу збережено в окремій резервній копії на сервері.")
    except Exception as e:
        logging.exception("Restore failed")
        await m.answer("⚠️ Не вдалося відновити базу: "+html.escape(str(e)[:400]))
    finally:
        if os.path.exists(temp): os.remove(temp)

@router.message(Command("whitelist"))
async def cmd_whitelist(m: Message):
    rows=db.execute("SELECT w.chat_id,c.name FROM whitelist w LEFT JOIN chat_names c ON c.owner_id=w.owner_id AND c.chat_id=w.chat_id WHERE w.owner_id=?",(m.from_user.id,)).fetchall()
    await m.answer("📋 <b>Дозволені контакти</b>\n\n"+("\n".join(f"• {html.escape(n or str(cid))} — <code>{cid}</code>" for cid,n in rows) if rows else "Список порожній. Команда !дозволити у чаті додає контакт."))

@router.message(Command("antiflood"))
async def cmd_antiflood(m: Message):
    parts=m.text.split(maxsplit=1)
    if len(parts)<2 or not parts[1].isdigit():
        await m.answer("Формат: <code>/antiflood 20</code> (повідомлень/хв), або <code>/antiflood 0</code> для вимкнення."); return
    limit=max(0,min(300,int(parts[1])))
    get_setting(m.from_user.id,"antiflood_limit")
    db.execute("UPDATE settings SET antiflood_limit=? WHERE owner_id=?",(limit,m.from_user.id)); db.commit()
    await m.answer(f"🛡 Антифлуд: {'вимкнено' if not limit else str(limit)+' повідомлень за хвилину'}.")

@router.message(Command("autotranslate"))
async def cmd_autotranslate(m: Message):
    parts=m.text.split(maxsplit=1); arg=parts[1].strip() if len(parts)>1 else "on uk"
    get_setting(m.from_user.id,"autotranslate_enabled")
    if arg.lower()=="off":
        db.execute("UPDATE settings SET autotranslate_enabled=0 WHERE owner_id=?",(m.from_user.id,)); db.commit(); await m.answer("Автопереклад вимкнено."); return
    lang=arg.split()[-1] if arg.lower().startswith("on ") else arg
    db.execute("UPDATE settings SET autotranslate_enabled=1,autotranslate_lang=? WHERE owner_id=?",(lang,m.from_user.id)); db.commit()
    await m.answer(f"🌐 Автопереклад увімкнено. Мова: {html.escape(lang)}. Для вимкнення: /autotranslate off")

@router.message(Command("autonote"))
async def cmd_autonote(m: Message):
    parts=m.text.split(maxsplit=1)
    if len(parts)<2: await m.answer("Формат: <code>/autonote ключове_слово</code> або <code>/autonote off</code>"); return
    value="" if parts[1].strip().lower()=="off" else parts[1].strip()
    db.execute("UPDATE settings SET autonote_keyword=? WHERE owner_id=?",(value,m.from_user.id)); db.commit()
    await m.answer("Автоматичні нотатки вимкнено." if not value else f"📝 Зберігатиму повідомлення, що містять «{html.escape(value)}».")

@router.message(Command("autotag"))
async def cmd_autotag(m: Message):
    parts=(m.text or "").split(maxsplit=2)
    if len(parts)<2:
        await m.answer("Формат: <code>/autotag ключове_слово назва_мітки</code> або <code>/autotag off</code>"); return
    if parts[1].lower()=="off":
        db.execute("DELETE FROM auto_rules WHERE owner_id=? AND kind='tag'",(m.from_user.id,)); db.commit()
        await m.answer("Автоматичне тегування вимкнено."); return
    if len(parts)<3:
        await m.answer("Формат: <code>/autotag ключове_слово назва_мітки</code>"); return
    db.execute("INSERT OR REPLACE INTO auto_rules(owner_id,kind,value,enabled) VALUES(?,?,?,1)",(m.from_user.id,"tag",parts[1]+"|"+parts[2])); db.commit()
    await m.answer(f"🏷 Автотегування увімкнено: слово «{html.escape(parts[1])}» → мітка «{html.escape(parts[2])}».")

@router.message(Command("tags"))
async def cmd_tags(m: Message):
    rows=db.execute("SELECT t.chat_id,t.tag,c.name FROM tags t LEFT JOIN chat_names c ON c.owner_id=t.owner_id AND c.chat_id=t.chat_id WHERE t.owner_id=? ORDER BY t.tag",(m.from_user.id,)).fetchall()
    await m.answer("🏷 <b>Мітки контактів</b>\n\n"+("\n".join(f"• {html.escape(n or str(cid))}: {html.escape(tag)}" for cid,tag,n in rows) if rows else "Міток ще немає."))

@router.message(Command("rules"))
async def cmd_rules(m: Message):
    await m.answer("⚙️ <b>Активні правила</b>\n\n"+
      f"Антифлуд: {get_setting_value(m.from_user.id,'antiflood_limit') or 0} повідомлень/хв\n"+
      f"Автопереклад: {'так' if get_setting(m.from_user.id,'autotranslate_enabled') else 'ні'}\n"+
      f"Автонотатка: {html.escape(get_setting_value(m.from_user.id,'autonote_keyword') or 'вимкнено')}\n"+
      f"DND: {'так' if get_setting(m.from_user.id,'dnd_enabled') else 'ні'}")


@router.message(Command("daily"))
async def cmd_daily(m: Message):
    since=time.time()-86400
    rows=db.execute("SELECT event,COUNT(*) FROM stats_log WHERE owner_id=? AND ts>=? GROUP BY event",(m.from_user.id,since)).fetchall()
    await m.answer("📅 <b>Підсумок за останні 24 години</b>\n\n"+("\n".join(f"• {e}: {n}" for e,n in rows) if rows else "Подій не було."))

@router.message(Command("schedule"))
async def cmd_schedule(m: Message):
    await cmd_tasks(m)

@router.message(Command("blocked"))
async def cmd_blocked(m: Message):
    rows=db.execute("SELECT chat_id,chat_name,until FROM muted WHERE owner_id=? ORDER BY chat_name",(m.from_user.id,)).fetchall()
    if not rows:
        await m.answer("🔇 Список замучених контактів порожній."); return
    lines=[]
    for cid,name,until in rows:
        ending="назавжди" if not until else datetime.fromtimestamp(until).strftime("до %d.%m %H:%M")
        lines.append(f"• {html.escape(name or get_chat_display_name(m.from_user.id,cid))} — {ending} — <code>{cid}</code>")
    await m.answer("🔇 <b>Замучені контакти</b>\n\n"+"\n".join(lines))

@router.message(Command("antispam"))
async def cmd_antispam(m: Message):
    get_setting(m.from_user.id,"antispam_enabled")
    db.execute("UPDATE settings SET antispam_enabled=1-antispam_enabled WHERE owner_id=?",(m.from_user.id,)); db.commit()
    state=get_setting(m.from_user.id,"antispam_enabled")
    await m.answer("🛡 Антиспам увімкнено: дублікати повідомлень будуть позначатися." if state else "Антиспам вимкнено.")

@router.message(Command("viponly"))
async def cmd_viponly(m: Message):
    get_setting(m.from_user.id,"vip_only")
    db.execute("UPDATE settings SET vip_only=1-vip_only WHERE owner_id=?",(m.from_user.id,)); db.commit()
    await m.answer("⭐ Тепер сповіщення надходитимуть лише від VIP." if get_setting(m.from_user.id,"vip_only") else "Сповіщення знову надходитимуть від усіх контактів (з урахуванням DND).")


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
        "🛠 <b>Налаштування</b>\n\nНатискай кнопки, щоб змінити параметри. Для значень, які потребують введення тексту або часу, скористайся відповідною командою з розділу «Команди».",
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


def help_commands_keyboard(category: str = "root") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if category == "root":
        kb.button(text="👤 Команди в особистих чатах", callback_data="help:cmds_private")
        kb.button(text="🤖 Команди в чаті з ботом", callback_data="help:cmds_bot")
        kb.button(text="👥 Команди в групах", callback_data="help:cmds_group")
        kb.button(text="« Назад", callback_data="menu:main")
    else:
        if category == "cmds_bot":
            kb.button(text="Наступна сторінка →", callback_data="help:cmds_bot2")
        kb.button(text="« До всіх команд", callback_data="help:cmds")
        kb.button(text="🏠 Головне меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("help:"))
async def cb_help(c: CallbackQuery, bot: Bot):
    page = c.data.split(":", 1)[1]
    if page == "cmds":
        await c.message.edit_text(
            "📚 <b>Команди</b>\n\nОбери, де саме потрібно виконати команду:",
            reply_markup=help_commands_keyboard("root"),
        )
        await c.answer()
        return
    if page in ("cmds_private", "cmds_bot", "cmds_bot2", "cmds_group"):
        raw = HELP_PAGES["cmds"]
        if page == "cmds_private":
            begin = raw.find("<b>🔇 Команди в особистих чатах</b>")
            finish = raw.find("<b>🤖 Команди в чаті з ботом</b>")
            body = raw[begin:finish].strip()
        elif page in ("cmds_bot", "cmds_bot2"):
            begin = raw.find("<b>🤖 Команди в чаті з ботом</b>")
            bot_commands = raw[begin:].strip()
            lines = bot_commands.splitlines()
            chunks, chunk = [], ""
            for line in lines:
                candidate = (chunk + "\n" + line).strip()
                if len(candidate) > 3500 and chunk:
                    chunks.append(chunk)
                    chunk = line
                else:
                    chunk = candidate
            if chunk: chunks.append(chunk)
            body = chunks[1] if page == "cmds_bot2" and len(chunks) > 1 else chunks[0]
        else:
            body = (
                "👥 <b>Команди керування групою</b>\n\n"
                "<code>/help</code> — довідка з керування групою.\n"
                "<code>/info</code> — інформація про групу та права бота.\n"
                "<code>/admins</code> — список адміністраторів.\n"
                "<code>/title НАЗВА</code> — змінити назву групи.\n"
                "<code>/description ТЕКСТ</code> — змінити опис групи.\n"
                "<code>/ban</code> — заблокувати учасника (відповіддю на його повідомлення).\n"
                "<code>/unban USER_ID</code> — розблокувати учасника.\n"
                "<code>/kick</code> — видалити учасника (відповіддю на повідомлення).\n"
                "<code>/mute 10m</code> — заборонити писати на час (відповіддю).\n"
                "<code>/unmute</code> — зняти обмеження (відповіддю).\n"
                "<code>/purge 20</code> — видалити до 20 повідомлень, починаючи з відповіді.\n"
                "<code>/pin</code> — закріпити повідомлення (відповіддю).\n"
                "<code>/unpin</code> — відкріпити повідомлення (відповіддю).\n\n"
                "Бот має бути адміністратором із відповідними правами. Рівні адміністраторів бота налаштовуються окремо від статусу адміністратора Telegram."
            )
        menu_category = "cmds_bot" if page == "cmds_bot" else page
        await c.message.edit_text(body, reply_markup=help_commands_keyboard(menu_category))
        await c.answer()
        return
    text = HELP_PAGES[page].replace("{bot_username}", (await bot.me()).username)
    await c.message.edit_text(text, reply_markup=back_keyboard(page))
    await c.answer()


@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle(c: CallbackQuery):
    toggle_setting(c.from_user.id, c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=settings_keyboard(c.from_user.id))
    await c.answer("Збережено")


@router.callback_query(F.data == "setting:antiflood")
async def cb_setting_antiflood(c: CallbackQuery):
    await c.answer("Надішли /antiflood 20 (або /antiflood 0 для вимкнення).", show_alert=True)


@router.callback_query(F.data == "setting:cleanup")
async def cb_setting_cleanup(c: CallbackQuery):
    await c.answer("Надішли /cleanup 30, щоб зберігати історію 30 днів.", show_alert=True)


@router.callback_query(F.data == "setting:translate_lang")
async def cb_setting_translate(c: CallbackQuery):
    await c.answer("Надішли /autotranslate on uk або /autotranslate off.", show_alert=True)


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
        if len(parts) > 1 and duration is None:
            await bot.send_message(owner_chat, "Формат часу: 10хв, 1г, 1д, 1м або 1р.")
            return
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
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Користувача замучено", (parts[1] if len(parts)>1 else "Назавжди"))

    elif lower == "!анмут":
        db.execute("DELETE FROM muted WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Мут знято")

    elif lower == "!віп":
        db.execute("INSERT OR IGNORE INTO vip VALUES(?,?)", (owner_id, m.chat.id))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Додано VIP", "Сповіщення проходитимуть навіть у DND")

    elif lower == "!анвіп":
        db.execute("DELETE FROM vip WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Статус VIP знято")

    elif lower == "!ігнор":
        db.execute("INSERT OR IGNORE INTO ignored_chats VALUES(?,?)", (owner_id, m.chat.id))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Чат додано до ігнорованих")

    elif lower == "!деігнор":
        db.execute(
            "DELETE FROM ignored_chats WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id)
        )
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Чат прибрано з ігнорованих")

    elif lower == "!дозволити":
        db.execute("INSERT OR IGNORE INTO whitelist VALUES(?,?)", (owner_id, m.chat.id))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Контакт додано до дозволених")

    elif lower == "!заборонити":
        db.execute("DELETE FROM whitelist WHERE owner_id=? AND chat_id=?", (owner_id, m.chat.id))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Контакт прибрано зі списку дозволених")

    elif lower == "!антиспам":
        get_setting(owner_id, "antispam_enabled")
        db.execute("UPDATE settings SET antispam_enabled=1-antispam_enabled WHERE owner_id=?", (owner_id,))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Перемкнуто антиспам у налаштуваннях", "Перемикач діє для всього акаунта")

    elif lower.startswith("!тихийрежим"):
        parts = text.split(maxsplit=1)
        duration = parse_duration(parts[1]) if len(parts)>1 else None
        if len(parts) > 1 and duration is None:
            await bot.send_message(owner_chat, "Формат часу: 10хв, 1г, 1д, 1м або 1р.")
            return
        if duration and duration >= 86400:
            await bot.send_message(owner_chat,"Для !тихийрежим вкажи тривалість до 23 год 59 хв.")
            return
        get_setting(owner_id, "dnd_enabled")
        if duration:
            start_clock=datetime.now().strftime("%H:%M")
            end_clock=(datetime.now()+timedelta(seconds=duration)).strftime("%H:%M")
            db.execute("UPDATE settings SET dnd_enabled=1,dnd_start=?,dnd_end=? WHERE owner_id=?", (start_clock,end_clock,owner_id))
            db.execute("INSERT INTO scheduled_tasks(owner_id,chat_id,conn_id,type,run_at,payload) VALUES(?,?,?,?,?,?)",
                       (owner_id,None,None,"dnd_off",time.time()+duration,None))
        else:
            db.execute("UPDATE settings SET dnd_enabled=1 WHERE owner_id=?", (owner_id,))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Увімкнено тихий режим", parts[1] if len(parts)>1 else "До ручного вимкнення")

    elif lower == "!тількивіп":
        get_setting(owner_id, "vip_only")
        db.execute("UPDATE settings SET vip_only=1-vip_only WHERE owner_id=?", (owner_id,))
        db.commit()
        await notify_contact(bot, owner_chat, owner_id, m.chat.id, "Перемкнено режим лише важливих контактів")

    elif lower.startswith("!повтор "):
        parts=text.split(maxsplit=1)
        duration=parse_duration(parts[1]) if len(parts)>1 else None
        if not duration:
            await bot.send_message(owner_chat,"Формат: <code>!повтор 10хв</code>")
        else:
            db.execute("INSERT INTO scheduled_tasks(owner_id,chat_id,conn_id,type,run_at,payload) VALUES(?,?,?,?,?,?)",
                       (owner_id,m.chat.id,m.business_connection_id,"notify_repeat",time.time()+duration,"Повторне нагадування про контакт"))
            db.commit()
            await notify_contact(bot,owner_chat,owner_id,m.chat.id,"Заплановано повторне сповіщення",parts[1])

    elif lower.startswith("!закріпити "):
        note_text=text.split(maxsplit=1)[1]
        db.execute("INSERT INTO notes(owner_id,text,created_at) VALUES(?,?,?)",(owner_id,note_text,time.time())); db.commit()
        await notify_contact(bot,owner_chat,owner_id,m.chat.id,"Текст збережено як нотатку")

    elif lower == "!цитата":
        row=db.execute("SELECT text,from_name FROM messages WHERE conn_id=? AND chat_id=? ORDER BY created_at DESC LIMIT 1",(m.business_connection_id,m.chat.id)).fetchone()
        if row:
            await bot.send_message(owner_chat,contact_card(owner_id,m.chat.id,"Останнє збережене повідомлення")+"\n\n"+f"<b>{html.escape(row[1] or '')}:</b>\n{html.escape(row[0] or '[медіа]')}")
        else:
            await bot.send_message(owner_chat,"Для цього контакту ще немає збережених повідомлень.")

    elif lower == "!очиститиісторію":
        db.execute("DELETE FROM messages WHERE conn_id=? AND chat_id=?",(m.business_connection_id,m.chat.id))
        db.execute("DELETE FROM message_versions WHERE conn_id=? AND chat_id=?",(m.business_connection_id,m.chat.id))
        db.commit()
        await notify_contact(bot,owner_chat,owner_id,m.chat.id,"Локальну збережену історію очищено")

    elif lower.startswith("!автовидалення "):
        parts=text.split(maxsplit=1); duration=parse_duration(parts[1]) if len(parts)>1 else None
        if duration is None or (duration and duration < 3600):
            await bot.send_message(owner_chat,"Формат: <code>!автовидалення 24г</code>, <code>!автовидалення 1д</code> або <code>!автовидалення 0г</code> для вимкнення")
        else:
            hours=(duration + 3599)//3600
            get_setting(owner_id,"auto_delete_hours")
            db.execute("UPDATE settings SET auto_delete_hours=? WHERE owner_id=?",(hours,owner_id)); db.commit()
            await notify_contact(bot,owner_chat,owner_id,m.chat.id,"Змінено термін зберігання копій",f"{hours} годин; 0 вимикає")

    elif lower.startswith("!нотатка "):
        note_text = text.split(maxsplit=1)[1]
        db.execute(
            "INSERT INTO notes(owner_id, text, created_at) VALUES(?,?,?)",
            (owner_id, note_text, time.time()),
        )
        db.commit()
        await bot.send_message(owner_chat, contact_card(owner_id,m.chat.id,"Збережено нотатку") + f"\n\n📝 {html.escape(note_text)}")

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
                    owner_chat, contact_card(owner_id,m.chat.id,"Створено нагадування",f"{when_label}, {hhmm}") + f"\n\n⏰ {html.escape(rtext)}"
                )

    elif lower.startswith("!через "):
        rest = text.split(maxsplit=1)[1]
        rparts = rest.split(maxsplit=1)
        duration = parse_duration(rparts[0]) if rparts else None
        if not duration or len(rparts) < 2:
            await bot.send_message(owner_chat, "Формат: <code>!через 10хв текст</code>")
        else:
            db.execute(
                "INSERT INTO scheduled_tasks(owner_id, chat_id, conn_id, type, run_at, payload) "
                "VALUES(?,?,?,?,?,?)",
                (owner_id, m.chat.id, m.business_connection_id, "delayed_message",
                 time.time() + duration, rparts[1]),
            )
            db.commit()
            await bot.send_message(owner_chat, contact_card(owner_id,m.chat.id,"Заплановано відкладене повідомлення",rparts[0]))

    elif lower.startswith("!шаблон "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await bot.send_message(owner_chat, "Формат: <code>!шаблон назва текст</code>")
        else:
            _, name, body = parts
            db.execute("INSERT OR REPLACE INTO templates VALUES(?,?,?)", (owner_id, name, body))
            db.commit()
            await bot.send_message(owner_chat, contact_card(owner_id,m.chat.id,"Збережено шаблон відповіді",name))

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
        await bot.send_message(owner_chat, contact_card(owner_id,m.chat.id,"Обмежено ключове слово цим чатом",word))

    elif lower.startswith("!ключвиняток "):
        word = text.split(maxsplit=1)[1].strip()
        db.execute(
            "INSERT OR REPLACE INTO keyword_scope VALUES(?,?,?,?)",
            (owner_id, word, m.chat.id, "exclude"),
        )
        db.commit()
        await bot.send_message(owner_chat, contact_card(owner_id,m.chat.id,"Вимкнено ключове слово для цього чату",word))

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
                    owner_chat, contact_card(owner_id,m.chat.id,"Персональну автовідповідь вимкнено")
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO autoreply_contacts VALUES(?,?,?)",
                    (owner_id, m.chat.id, arg),
                )
                await bot.send_message(
                    owner_chat, contact_card(owner_id,m.chat.id,"Налаштовано персональну автовідповідь",arg)
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
    conn = await resolve_business_connection(bot, m.business_connection_id)
    if not conn:
        logging.warning("Ignoring business message: no owner mapping for connection %s", m.business_connection_id)
        return
    owner_id, owner_chat = conn

    if m.from_user and m.from_user.id == owner_id:
        await handle_owner_command(m, bot, owner_id, owner_chat)
        return

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

    if m.from_user:
        remember_contact(owner_id, m)
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
            await bot.send_message(owner_chat, contact_card(owner_id, m.chat.id, "Повідомлення відредаговано") + "\n\n" + "\n\n".join(lines))
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
            # Окрема стандартна картка контакту для однакового формату сповіщень
            await bot.send_message(owner_chat, contact_card(owner_id, m.chat.id, "Повідомлення відредаговано"))
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
        db.execute(
            "INSERT INTO stats_log(owner_id, event, ts, chat_id) VALUES(?,?,?,?)",
            (owner_id, "deleted", time.time(), d.chat.id),
        )
        if not row:
            continue
        deleted_rows.append(row)
        # Якщо контакт ще не був у новій таблиці, відновлюємо картку з метаданих збереженого повідомлення.
        name, saved_text, media_type, saved_file_id, saved_user_id, saved_username = row
        if saved_user_id:
            db.execute("INSERT OR REPLACE INTO contacts(owner_id,chat_id,user_id,username,full_name,updated_at) VALUES(?,?,?,?,?,?)",
                       (owner_id,d.chat.id,saved_user_id,saved_username,name,time.time()))
            db.execute("INSERT OR REPLACE INTO chat_names(owner_id,chat_id,name) VALUES(?,?,?)",
                       (owner_id,d.chat.id,name or str(d.chat.id)))


        if notify:
            name, text, mtype, fid, sender_id, sender_username = row
            sender = format_sender(name, sender_id if sender_id else "?", sender_username)
            footer = f"\n\n@{BOT_USERNAME}" if BOT_USERNAME else ""
            header = f"{sender} видалив(ла) повідомлення:"
            if text:
                body = contact_card(owner_id, d.chat.id, "Повідомлення видалено") + f"\n\n{header}\n\n<blockquote>{html.escape(text)}</blockquote>{footer}"
            else:
                body = contact_card(owner_id, d.chat.id, "Повідомлення видалено") + f"\n\n{header}{footer}"

            if mtype and fid and get_setting(owner_id, "log_media"):
                try:
                    await bot.send_message(owner_chat, contact_card(owner_id, d.chat.id, "Повідомлення з медіа видалено") + "\n\n" + header + footer)
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
            "INSERT INTO deleted_archive(owner_id,conn_id,chat_id,msg_id,from_name,text,media_type,file_id,from_id,from_username,deleted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (owner_id, d.business_connection_id, d.chat.id, msg_id,
             row[0], row[1], row[2], row[3], row[4], row[5], time.time()),
        )
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
                            await bot.send_message(owner_chat, contact_card(owner_id, chat_id, "Мут автоматично знято", "Час дії завершився"))
                    elif ttype == "dnd_off":
                        db.execute("UPDATE settings SET dnd_enabled=0 WHERE owner_id=?", (owner_id,))
                        if owner_chat:
                            await bot.send_message(owner_chat, "🌙 Тихий режим автоматично вимкнено.")
                    elif ttype == "notify_repeat" and owner_chat and chat_id:
                        await bot.send_message(owner_chat, contact_card(owner_id, chat_id, "Повторне сповіщення", payload or ""))
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
                        "DELETE FROM message_versions WHERE created_at < ? AND conn_id IN "
                        "(SELECT conn_id FROM connections WHERE owner_id=?)",
                        (cutoff, owner_id),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO daily_tasks VALUES(?,?,?)",
                        (owner_id, "cleanup", today),
                    )

                # Додатковий короткий термін зберігання копій (години), якщо задано.
                hours = int(get_setting_value(owner_id, "auto_delete_hours") or 0)
                if hours > 0:
                    cutoff_hours = now_ts - hours * 3600
                    db.execute("DELETE FROM messages WHERE created_at < ? AND conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?)", (cutoff_hours, owner_id))
                    db.execute("DELETE FROM message_versions WHERE created_at < ? AND conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?)", (cutoff_hours, owner_id))

                # Автоматична резервна копія щопонеділка
                if datetime.now().weekday() == 0 and get_setting(owner_id, "auto_backup_enabled"):
                    last_b = db.execute("SELECT last_date FROM daily_tasks WHERE owner_id=? AND task='auto_backup'", (owner_id,)).fetchone()
                    if (not last_b or last_b[0] != today) and owner_chat:
                        backup_path=f"auto_backup_{owner_id}_{int(now_ts)}.db"
                        backup_db=sqlite3.connect(backup_path)
                        try:
                            db.commit(); db.backup(backup_db)
                        finally:
                            backup_db.close()
                        try:
                            await bot.send_document(owner_chat, FSInputFile(backup_path), caption="🛡 Щотижнева автоматична резервна копія бази даних.")
                        finally:
                            if os.path.exists(backup_path): os.remove(backup_path)
                        db.execute("INSERT OR REPLACE INTO daily_tasks VALUES(?,?,?)", (owner_id,"auto_backup",today))

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
            # Compact SQLite after cleanup; this releases pages from deleted history.
            # Run once per day, and only after all write transactions are committed.
            last_vacuum = db.execute(
                "SELECT last_date FROM daily_tasks WHERE owner_id=0 AND task='db_vacuum'"
            ).fetchone()
            if not last_vacuum or last_vacuum[0] != today:
                db.execute("PRAGMA optimize")
                db.commit()
                db.execute("VACUUM")
                db.execute(
                    "INSERT OR REPLACE INTO daily_tasks VALUES(?,?,?)",
                    (0, "db_vacuum", today),
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

# ---------------------- РОЗШИРЕНІ ФУНКЦІЇ ----------------------
@router.message(Command("history"))
async def cmd_history(m: Message):
    parts=(m.text or "").split(maxsplit=1); owner=m.from_user.id
    if len(parts)<2:
        await m.answer("Формат: /history CHAT_ID [MESSAGE_ID]. Переглядає збережені версії повідомлень."); return
    args=parts[1].split(); chat_id=int(args[0]) if args[0].isdigit() else None
    if not chat_id:
        row=db.execute("SELECT chat_id FROM chat_names WHERE owner_id=? AND name LIKE ? LIMIT 1",(owner,f"%{args[0]}%")).fetchone()
        chat_id=row[0] if row else None
    if not chat_id: await m.answer("Контакт не знайдено."); return
    if len(args)>1 and args[1].isdigit():
        rows=db.execute("SELECT text,created_at FROM message_versions WHERE chat_id=? AND msg_id=? AND conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?) ORDER BY created_at",(chat_id,int(args[1]),owner)).fetchall()
    else:
        rows=db.execute("SELECT msg_id,text,created_at FROM message_versions WHERE chat_id=? AND conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?) ORDER BY created_at DESC LIMIT 30",(chat_id,owner)).fetchall()
        lines=[f"#{mid} · {datetime.fromtimestamp(ts).strftime('%d.%m %H:%M:%S')} · {html.escape((txt or '[медіа]')[:180])}" for mid,txt,ts in rows]
        await m.answer("🕘 <b>Останні збережені версії</b>\n\n"+("\n".join(lines) if lines else "Історія порожня.")+"\n\nДля всіх версій: /history CHAT_ID MESSAGE_ID"); return
    lines=[f"<b>Версія {i}</b> · {datetime.fromtimestamp(ts).strftime('%d.%m %H:%M:%S')}\n<blockquote>{html.escape(txt or '[медіа]')}</blockquote>" for i,(txt,ts) in enumerate(rows,1)]
    await m.answer("🕘 <b>Історія редагувань</b>\n\n"+("\n\n".join(lines) if lines else "Версій не знайдено."))

# ---------------------- МОДЕРАЦІЯ TELEGRAM-ГРУП ----------------------
def is_group_chat(m: Message) -> bool:
    return bool(m.chat and m.chat.type in ("group", "supergroup"))


# Рівні доступу: 1 — молодший адміністратор, 2 — модератор,
# 3 — старший модератор, 4 — заступник власника, 5 — власник групи.
GROUP_ROLE_NAMES = {
    1: "Молодший адміністратор", 2: "Модератор", 3: "Старший модератор",
    4: "Заступник власника", 5: "Власник групи",
}
GROUP_COMMAND_LEVELS = {
    "info": 1, "admins": 1, "roles": 1,
    "mute": 2, "unmute": 2, "kick": 2,
    "ban": 3, "unban": 3, "purge": 3,
    "pin": 3, "unpin": 3,
    "title": 4, "description": 4,
}

async def group_user_level(chat_id: int, user_id: int, bot: Bot) -> int:
    """Власник Telegram-групи завжди має рівень 5; інші — з БД."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status == "creator":
            return 5
    except Exception:
        return 0
    row = db.execute("SELECT level FROM group_roles WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    return int(row[0]) if row else 0

async def require_group_admin(m: Message, bot: Bot) -> bool:
    if not is_group_chat(m):
        await m.answer("Ця команда працює лише в групах і супергрупах.")
        return False
    command = (m.text or "").split(maxsplit=1)[0].split("@")[0].lstrip("/").lower()
    default_level = GROUP_COMMAND_LEVELS.get(command, 2)
    row = db.execute("SELECT min_level FROM group_command_access WHERE chat_id=? AND command=?", (m.chat.id, command)).fetchone()
    required_level = int(row[0]) if row else default_level
    try:
        actor_level = await group_user_level(m.chat.id, m.from_user.id, bot)
        if actor_level < required_level:
            await m.answer(f"⛔ Недостатній рівень доступу. Для /{html.escape(command)} потрібен рівень {required_level} ({GROUP_ROLE_NAMES[required_level]}). Ваш рівень: {actor_level}.")
            return False
        me = await bot.get_chat_member(m.chat.id, (await bot.me()).id)
        if me.status != "administrator":
            await m.answer("Спочатку додай бота в адміністратори групи з потрібними правами.")
            return False
    except Exception as e:
        await m.answer("Не вдалося перевірити права: " + html.escape(str(e)[:250]))
        return False
    return True

async def management_actor_level(m: Message, bot: Bot, minimum: int = 4) -> int:
    if not is_group_chat(m):
        await m.answer("Команда доступна лише в групі.")
        return 0
    level = await group_user_level(m.chat.id, m.from_user.id, bot)
    if level < minimum:
        await m.answer(f"⛔ Потрібен рівень {minimum} ({GROUP_ROLE_NAMES[minimum]}).")
        return 0
    try:
        me = await bot.get_chat_member(m.chat.id, (await bot.me()).id)
        if me.status != "administrator":
            await m.answer("Спочатку додай бота в адміністратори групи.")
            return 0
    except Exception as e:
        await m.answer("Не вдалося перевірити права бота: " + html.escape(str(e)[:200]))
        return 0
    return level

def command_target_id(m: Message, args: list[str]):
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id
    if args and args[0].lstrip("-").isdigit():
        return int(args[0])
    return None


def replied_user(m: Message):
    return m.reply_to_message.from_user if m.reply_to_message else None


@router.message(Command("roles"))
async def group_roles(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    if not is_group_chat(m):
        await m.answer("Команда доступна лише в групі."); return
    try:
        admins = await bot.get_chat_administrators(m.chat.id)
        lines = []
        owner = next((a for a in admins if a.status == "creator"), None)
        if owner:
            lines.append(f"• {html.escape(owner.user.full_name)} — рівень 5: {GROUP_ROLE_NAMES[5]}")
        rows = db.execute("SELECT user_id,level FROM group_roles WHERE chat_id=? ORDER BY level DESC,user_id", (m.chat.id,)).fetchall()
        for uid, level in rows:
            try:
                member = await bot.get_chat_member(m.chat.id, int(uid))
                lines.append(f"• {html.escape(member.user.full_name)} — рівень {level}: {GROUP_ROLE_NAMES[int(level)]}")
            except Exception:
                lines.append(f"• ID <code>{uid}</code> — рівень {level}: {GROUP_ROLE_NAMES[int(level)]} (не в групі або недоступний)")
        await m.answer("🏅 <b>Ієрархія адміністраторів бота</b>\n\n" + ("\n".join(lines) if lines else "Адміністраторів бота ще не призначено."))
    except Exception as e:
        await m.answer("Не вдалося отримати список: " + html.escape(str(e)[:250]))

@router.message(Command("adminadd"))
async def group_admin_add(m: Message, bot: Bot):
    actor = await management_actor_level(m, bot, 4)
    if not actor: return
    args = (m.text or "").split()[1:]
    target_id = command_target_id(m, args)
    level_arg = next((x for x in args if x.isdigit() and int(x) in range(1,5)), None)
    new_level = int(level_arg) if level_arg else 1
    if not target_id:
        await m.answer("Формат: /adminadd USER_ID [РІВЕНЬ 1-4] або відповідь на повідомлення."); return
    if new_level >= actor:
        await m.answer("Не можна призначити рівень, рівний або вищий за ваш."); return
    try:
        member = await bot.get_chat_member(m.chat.id, target_id)
        if member.status == "creator":
            await m.answer("Власник групи вже має рівень 5."); return
        db.execute("INSERT INTO group_roles(chat_id,user_id,level,added_by,created_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET level=excluded.level,added_by=excluded.added_by,created_at=excluded.created_at", (m.chat.id,target_id,new_level,m.from_user.id,time.time()))
        db.commit()
        await m.answer(f"✅ {html.escape(member.user.full_name)} призначено: рівень {new_level} — {GROUP_ROLE_NAMES[new_level]}.")
    except Exception as e:
        await m.answer("Не вдалося додати адміністратора. Перевір USER_ID та участь користувача в групі. " + html.escape(str(e)[:180]))

@router.message(Command("adminremove"))
async def group_admin_remove(m: Message, bot: Bot):
    actor = await management_actor_level(m, bot, 4)
    if not actor: return
    args = (m.text or "").split()[1:]
    target_id = command_target_id(m, args)
    if not target_id:
        await m.answer("Формат: /adminremove USER_ID або відповідь на повідомлення."); return
    target_level = await group_user_level(m.chat.id, target_id, bot)
    if target_level == 5 or target_level >= actor:
        await m.answer("Не можна зняти адміністратора з таким самим або вищим рівнем."); return
    db.execute("DELETE FROM group_roles WHERE chat_id=? AND user_id=?", (m.chat.id,target_id)); db.commit()
    await m.answer("✅ Права адміністратора бота знято." if target_level else "У цього користувача не було ролі адміністратора бота.")

async def change_group_role(m: Message, bot: Bot, delta: int):
    actor = await management_actor_level(m, bot, 4)
    if not actor: return
    args = (m.text or "").split()[1:]
    target_id = command_target_id(m, args)
    if not target_id:
        await m.answer("Вкажи USER_ID або відповідай на повідомлення користувача."); return
    old_level = await group_user_level(m.chat.id, target_id, bot)
    if old_level == 5 or old_level >= actor or old_level == 0:
        await m.answer("Цього користувача не можна змінити: він не має ролі або має рівень не нижчий за ваш."); return
    new_level = max(1, min(4, old_level + delta))
    if new_level == old_level:
        await m.answer("Досягнуто межі ієрархії (1–4). Рівень 5 зарезервовано власнику групи."); return
    db.execute("UPDATE group_roles SET level=?,added_by=?,created_at=? WHERE chat_id=? AND user_id=?", (new_level,m.from_user.id,time.time(),m.chat.id,target_id)); db.commit()
    await m.answer(f"✅ Рівень змінено: {old_level} → {new_level} ({GROUP_ROLE_NAMES[new_level]}).")

@router.message(Command("promote"))
async def group_promote(m: Message, bot: Bot):
    await change_group_role(m, bot, 1)

@router.message(Command("demote"))
async def group_demote(m: Message, bot: Bot):
    await change_group_role(m, bot, -1)

@router.message(Command("cmdaccess"))
async def group_command_access(m: Message, bot: Bot):
    actor = await management_actor_level(m, bot, 5)
    if not actor: return
    args = (m.text or "").split()[1:]
    if not args:
        rows = db.execute("SELECT command,min_level FROM group_command_access WHERE chat_id=? ORDER BY command", (m.chat.id,)).fetchall()
        overrides = {c:l for c,l in rows}
        lines = [f"/{cmd} — рівень {overrides.get(cmd, lvl)} (типово {lvl})" for cmd,lvl in sorted(GROUP_COMMAND_LEVELS.items())]
        await m.answer("⚙️ <b>Доступ до команд</b>\n\n" + "\n".join(lines) + "\n\nЗмінити: /cmdaccess КОМАНДА РІВЕНЬ (1–5).\nСкинути: /cmdaccess КОМАНДА default")
        return
    command = args[0].lstrip("/").lower()
    if command not in GROUP_COMMAND_LEVELS:
        await m.answer("Невідома команда. Переглянь список через /cmdaccess."); return
    if len(args) < 2:
        await m.answer("Формат: /cmdaccess КОМАНДА РІВЕНЬ (1–5) або default."); return
    if args[1].lower() == "default":
        db.execute("DELETE FROM group_command_access WHERE chat_id=? AND command=?", (m.chat.id,command)); db.commit()
        await m.answer(f"✅ Для /{command} відновлено типовий рівень {GROUP_COMMAND_LEVELS[command]}."); return
    if not args[1].isdigit() or int(args[1]) not in range(1,6):
        await m.answer("Рівень має бути числом від 1 до 5."); return
    level = int(args[1])
    db.execute("INSERT INTO group_command_access(chat_id,command,min_level,changed_by,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,command) DO UPDATE SET min_level=excluded.min_level,changed_by=excluded.changed_by,updated_at=excluded.updated_at", (m.chat.id,command,level,m.from_user.id,time.time())); db.commit()
    await m.answer(f"✅ Для /{command} встановлено мінімальний рівень {level} — {GROUP_ROLE_NAMES[level]}.")

@router.message(Command("help"))
async def group_help(m: Message):
    await m.answer(
        "👥 <b>Керування групою</b>\\n\\n"
        "Використовуй команди у відповідь на повідомлення учасника:\\n"
        "<code>/ban</code> — заблокувати; <code>/kick</code> — видалити; "
        "<code>/mute 10хв</code> — тимчасово обмежити; <code>/unmute</code> — зняти обмеження.\\n"
        "<code>/unban USER_ID</code> — розблокувати за ID.\\n"
        "<code>/purge 20</code> — видалити до 20 повідомлень від вибраного повідомлення назад.\\n"
        "<code>/pin</code> та <code>/unpin</code> — керувати закріпленням.\\n"
        "<code>/info</code> — перевірити групу та права бота.\\n"
        "<code>/admins</code> — список адміністраторів.\\n"
        "<code>/title НАЗВА</code> — змінити назву групи.\\n"
        "<code>/description ТЕКСТ</code> — змінити опис групи.\\n\\n"
        "Бот повинен бути адміністратором із дозволом блокувати/обмежувати учасників, видаляти та закріплювати повідомлення. Ролі бота не змінюють статус адміністратора Telegram."
    )


@router.message(Command("info"))
async def group_info(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    if not is_group_chat(m):
        await m.answer("Команда доступна лише в групі.")
        return
    try:
        me = await bot.get_chat_member(m.chat.id, (await bot.me()).id)
        await m.answer(
            f"👥 <b>{html.escape(m.chat.title or 'Група')}</b>\\n"
            f"Chat ID: <code>{m.chat.id}</code>\\n"
            f"Статус бота: <b>{html.escape(me.status)}</b>\\n"
            f"Бот може керувати учасниками: <b>{'так' if getattr(me, 'can_restrict_members', False) else 'ні'}</b>\\n"
            f"Бот може видаляти повідомлення: <b>{'так' if getattr(me, 'can_delete_messages', False) else 'ні'}</b>\\n"
            f"Бот може закріплювати: <b>{'так' if getattr(me, 'can_pin_messages', False) else 'ні'}</b>"
        )
    except Exception as e:
        await m.answer("Не вдалося отримати інформацію: " + html.escape(str(e)[:250]))


@router.message(Command("admins"))
async def group_admins(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    if not is_group_chat(m):
        await m.answer("Команда доступна лише в групі.")
        return
    try:
        admins = await bot.get_chat_administrators(m.chat.id)
        lines = [f"• {html.escape(a.user.full_name)}" + (f" (@{html.escape(a.user.username)})" if a.user.username else "") for a in admins]
        await m.answer("🛡 <b>Адміністратори групи</b>\\n\\n" + "\\n".join(lines))
    except Exception as e:
        await m.answer("Не вдалося отримати список: " + html.escape(str(e)[:250]))


@router.message(Command("title"))
async def group_title(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.answer("Формат: /title Нова назва групи")
        return
    try:
        await bot.set_chat_title(m.chat.id, parts[1].strip()[:128])
        await m.answer("✅ Назву групи змінено.")
    except Exception as e:
        await m.answer("Не вдалося змінити назву: " + html.escape(str(e)[:250]))


@router.message(Command("description"))
async def group_description(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Формат: /description Опис групи")
        return
    try:
        await bot.set_chat_description(m.chat.id, parts[1].strip()[:255])
        await m.answer("✅ Опис групи змінено.")
    except Exception as e:
        await m.answer("Не вдалося змінити опис: " + html.escape(str(e)[:250]))


@router.message(Command("ban"))
async def group_ban(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    target = replied_user(m)
    if not target:
        await m.answer("Відповідай командою /ban на повідомлення учасника.")
        return
    if target.id == m.from_user.id or target.id == (await bot.me()).id:
        await m.answer("Не можна заблокувати себе або самого бота.")
        return
    try:
        await bot.ban_chat_member(m.chat.id, target.id)
        await m.answer(f"🚫 Учасника {html.escape(target.full_name)} заблоковано.")
    except Exception as e:
        await m.answer("Не вдалося заблокувати: " + html.escape(str(e)[:250]))


@router.message(Command("unban"))
async def group_unban(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Формат: /unban USER_ID")
        return
    try:
        await bot.unban_chat_member(m.chat.id, int(parts[1]), only_if_banned=True)
        await m.answer("✅ Блокування знято.")
    except Exception as e:
        await m.answer("Не вдалося зняти блокування: " + html.escape(str(e)[:250]))


@router.message(Command("kick"))
async def group_kick(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    target = replied_user(m)
    if not target:
        await m.answer("Відповідай командою /kick на повідомлення учасника.")
        return
    try:
        await bot.ban_chat_member(m.chat.id, target.id)
        await bot.unban_chat_member(m.chat.id, target.id, only_if_banned=True)
        await m.answer(f"👢 Учасника {html.escape(target.full_name)} видалено з групи.")
    except Exception as e:
        await m.answer("Не вдалося видалити учасника: " + html.escape(str(e)[:250]))


@router.message(Command("mute"))
async def group_mute(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    target = replied_user(m)
    if not target:
        await m.answer("Відповідай командою /mute 10хв на повідомлення учасника.")
        return
    parts = (m.text or "").split(maxsplit=1)
    seconds = parse_duration(parts[1]) if len(parts) > 1 else 3600
    if not seconds or seconds < 60:
        await m.answer("Вкажи строк, наприклад /mute 30хв (мінімум 1 хв).")
        return
    until = datetime.now() + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            m.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False, can_send_audios=False,
                can_send_documents=False, can_send_photos=False, can_send_videos=False,
                can_send_video_notes=False, can_send_voice_notes=False,
                can_send_polls=False, can_send_other_messages=False,
                can_add_web_page_previews=False),
            until_date=until,
        )
        await m.answer(f"🔇 {html.escape(target.full_name)} не зможе писати до {until.strftime('%d.%m %H:%M')}.")
    except Exception as e:
        await m.answer("Не вдалося обмежити учасника: " + html.escape(str(e)[:250]))


@router.message(Command("unmute"))
async def group_unmute(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    target = replied_user(m)
    if not target:
        await m.answer("Відповідай командою /unmute на повідомлення учасника.")
        return
    try:
        chat = await bot.get_chat(m.chat.id)
        perms = chat.permissions or ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True,
            can_send_other_messages=True, can_add_web_page_previews=True)
        await bot.restrict_chat_member(m.chat.id, target.id, permissions=perms)
        await m.answer(f"🔊 Обмеження для {html.escape(target.full_name)} знято.")
    except Exception as e:
        await m.answer("Не вдалося зняти обмеження: " + html.escape(str(e)[:250]))


@router.message(Command("purge"))
async def group_purge(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    if not m.reply_to_message:
        await m.answer("Відповідай командою /purge 20 на повідомлення, з якого почати видалення.")
        return
    parts = (m.text or "").split(maxsplit=1)
    count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    count = max(1, min(count, 100))
    start_id = m.reply_to_message.message_id
    deleted = 0
    for message_id in range(start_id, start_id + count):
        try:
            await bot.delete_message(m.chat.id, message_id)
            deleted += 1
        except Exception:
            pass
    try: await bot.delete_message(m.chat.id, m.message_id)
    except Exception: pass
    await m.answer(f"🧹 Видалено повідомлень: {deleted}.")


@router.message(Command("pin"))
async def group_pin(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    if not m.reply_to_message:
        await m.answer("Відповідай /pin на повідомлення, яке потрібно закріпити.")
        return
    try:
        await bot.pin_chat_message(m.chat.id, m.reply_to_message.message_id)
        await m.answer("📌 Повідомлення закріплено.")
    except Exception as e:
        await m.answer("Не вдалося закріпити: " + html.escape(str(e)[:250]))


@router.message(Command("unpin"))
async def group_unpin(m: Message, bot: Bot):
    if not await require_group_admin(m, bot): return
    try:
        if m.reply_to_message:
            await bot.unpin_chat_message(m.chat.id, m.reply_to_message.message_id)
        else:
            await bot.unpin_chat_message(m.chat.id)
        await m.answer("📌 Закріплення знято.")
    except Exception as e:
        await m.answer("Не вдалося відкріпити: " + html.escape(str(e)[:250]))


@router.message(Command("group"))
async def cmd_group(m: Message):
    parts=(m.text or "").split(maxsplit=2); owner=m.from_user.id
    if len(parts)<2:
        rows=db.execute("SELECT group_name,COUNT(*) FROM contact_groups WHERE owner_id=? GROUP BY group_name ORDER BY group_name",(owner,)).fetchall()
        await m.answer("👥 <b>Групи контактів</b>\n\n"+("\n".join(f"• {html.escape(n)} — {c}" for n,c in rows) if rows else "Груп поки немає.")+"\n\n/group add НАЗВА CHAT_ID\n/group list НАЗВА\n/group del НАЗВА CHAT_ID"); return
    action=parts[1].lower()
    if action=="add" and len(parts)==3:
        bits=parts[2].rsplit(maxsplit=1)
        if len(bits)!=2 or not bits[1].lstrip('-').isdigit(): await m.answer("Формат: /group add НАЗВА CHAT_ID"); return
        db.execute("INSERT OR IGNORE INTO contact_groups VALUES(?,?,?)",(owner,bits[0],int(bits[1]))); db.commit(); await m.answer("✅ Контакт додано до групи.")
    elif action=="del" and len(parts)==3:
        bits=parts[2].rsplit(maxsplit=1)
        if len(bits)!=2 or not bits[1].lstrip('-').isdigit(): await m.answer("Формат: /group del НАЗВА CHAT_ID"); return
        db.execute("DELETE FROM contact_groups WHERE owner_id=? AND group_name=? AND chat_id=?",(owner,bits[0],int(bits[1]))); db.commit(); await m.answer("Контакт вилучено з групи.")
    elif action=="list" and len(parts)==3:
        rows=db.execute("SELECT g.chat_id,COALESCE(c.name,g.chat_id) FROM contact_groups g LEFT JOIN chat_names c ON c.owner_id=g.owner_id AND c.chat_id=g.chat_id WHERE g.owner_id=? AND g.group_name=?",(owner,parts[2])).fetchall()
        await m.answer(f"👥 <b>{html.escape(parts[2])}</b>\n\n"+("\n".join(f"• {html.escape(str(name))} — <code>{cid}</code>" for cid,name in rows) if rows else "Група порожня."))
    else: await m.answer("Команди: /group add НАЗВА CHAT_ID, /group list НАЗВА, /group del НАЗВА CHAT_ID")

@router.message(Command("contact"))
async def cmd_contact(m: Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: await m.answer("Формат: /contact CHAT_ID"); return
    arg=parts[1].strip(); owner=m.from_user.id
    row=db.execute("SELECT chat_id FROM chat_names WHERE owner_id=? AND name LIKE ? LIMIT 1",(owner,f"%{arg}%")).fetchone() if not arg.lstrip('-').isdigit() else None
    cid=row[0] if row else (int(arg) if arg.lstrip('-').isdigit() else None)
    if not cid: await m.answer("Контакт не знайдено."); return
    name,uid,username,url=stats_contact_link(owner,cid)
    counts={e:db.execute("SELECT COUNT(*) FROM stats_log WHERE owner_id=? AND chat_id=? AND event=?",(owner,cid,e)).fetchone()[0] for e in ("received","edited","deleted")}
    tags=db.execute("SELECT tag FROM tags WHERE owner_id=? AND chat_id=?",(owner,cid)).fetchone()
    groups=db.execute("SELECT group_name FROM contact_groups WHERE owner_id=? AND chat_id=?",(owner,cid)).fetchall()
    last=db.execute("SELECT text,created_at FROM messages WHERE chat_id=? AND conn_id IN (SELECT conn_id FROM connections WHERE owner_id=?) ORDER BY created_at DESC LIMIT 1",(cid,owner)).fetchone()
    link=f'\n<a href="{html.escape(url,quote=True)}">Відкрити профіль</a>' if url else ""
    await m.answer(f"👤 <b>Картка контакту</b>\n\n<b>{html.escape(str(name))}</b>\nID: <code>{uid or cid}</code>\nUsername: @{html.escape(username or 'невідомий')}\nПовідомлень: {counts['received']}\nРедагувань: {counts['edited']}\nВидалень: {counts['deleted']}\nМітка: {html.escape(tags[0] if tags else 'немає')}\nГрупи: {html.escape(', '.join(x[0] for x in groups) or 'немає')}\nОстаннє повідомлення: {html.escape((last[0] or '[медіа]')[:500] if last else 'немає')}"+link)

@router.message(Command("dashboard"))
async def cmd_dashboard(m: Message):
    owner=m.from_user.id
    contacts=db.execute("SELECT COUNT(*) FROM contacts WHERE owner_id=?",(owner,)).fetchone()[0]
    groups=db.execute("SELECT COUNT(DISTINCT group_name) FROM contact_groups WHERE owner_id=?",(owner,)).fetchone()[0]
    tasks=db.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE owner_id=? AND done=0",(owner,)).fetchone()[0]
    archived=db.execute("SELECT COUNT(*) FROM deleted_archive WHERE owner_id=?",(owner,)).fetchone()[0]
    fraud=db.execute("SELECT COUNT(*) FROM fraud_alerts WHERE owner_id=?",(owner,)).fetchone()[0]
    await m.answer(f"🧭 <b>Панель керування</b>\n\n👥 Контактів: <b>{contacts}</b>\n📁 Груп контактів: <b>{groups}</b>\n⏰ Активних завдань: <b>{tasks}</b>\n🗑 В архіві видалень: <b>{archived}</b>\n⚠️ Попереджень безпеки: <b>{fraud}</b>\n\n/history CHAT_ID — історія версій\n/group — групи контактів\n/contact CHAT_ID — картка контакту\n/archive — архів видалених\n/fraud — попередження безпеки\n/backups — історія резервних копій")

@router.message(Command("archive"))
async def cmd_archive(m: Message):
    parts=(m.text or "").split(maxsplit=1); owner=m.from_user.id
    rows=db.execute("SELECT id,chat_id,msg_id,from_name,text,media_type,deleted_at FROM deleted_archive WHERE owner_id=? ORDER BY deleted_at DESC LIMIT 20",(owner,)).fetchall()
    if len(parts)>1 and parts[1].strip().lower()=="clear":
        db.execute("DELETE FROM deleted_archive WHERE owner_id=?",(owner,)); db.commit(); await m.answer("Архів видалених повідомлень очищено."); return
    lines=[]
    for i,cid,mid,name,txt,mt,ts in rows:
        preview = txt or ("[" + str(mt or "медіа") + "]")
        lines.append(f"#{i} · {html.escape(str(name or 'Невідомо'))} · чат <code>{cid}</code> · {datetime.fromtimestamp(ts).strftime('%d.%m %H:%M')}\n{html.escape(preview[:220])}")
    await m.answer("🗑 <b>Архів видалених повідомлень</b>\n\n"+("\n\n".join(lines) if lines else "Архів порожній.")+"\n\n/archive clear — очистити архів")

@router.message(Command("smartremind"))
async def cmd_smartremind(m: Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: await m.answer("Формат: /smartremind HH:MM текст або /smartremind завтра HH:MM текст"); return
    raw=parts[1].strip(); day_offset=0
    if raw.lower().startswith("завтра "): day_offset=1; raw=raw[7:]
    bits=raw.split(maxsplit=1)
    if len(bits)<2 or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d",bits[0]): await m.answer("Вкажи час у форматі HH:MM і текст нагадування."); return
    hh,mm=map(int,bits[0].split(':')); run=(datetime.now()+timedelta(days=day_offset)).replace(hour=hh,minute=mm,second=0,microsecond=0)
    if run.timestamp()<=time.time(): await m.answer("Цей час уже минув. Вкажи майбутній час."); return
    db.execute("INSERT INTO scheduled_tasks(owner_id,chat_id,conn_id,type,run_at,payload,done) VALUES(?,?,NULL,'reminder',?,?,0)",(m.from_user.id,m.chat.id,run.timestamp(),bits[1])); db.commit()
    await m.answer(f"🧠 Розумне нагадування створено на {run.strftime('%d.%m о %H:%M')}: {html.escape(bits[1])}")

@router.message(Command("fraud"))
async def cmd_fraud(m: Message):
    rows=db.execute("SELECT chat_id,reason,content,created_at FROM fraud_alerts WHERE owner_id=? ORDER BY created_at DESC LIMIT 15",(m.from_user.id,)).fetchall()
    await m.answer("🛡 <b>Попередження про можливе шахрайство</b>\n\n"+("\n\n".join(f"Чат <code>{cid}</code> · {datetime.fromtimestamp(ts).strftime('%d.%m %H:%M')}\n<b>{html.escape(reason)}</b>\n{html.escape((content or '')[:300])}" for cid,reason,content,ts in rows) if rows else "Підозрілих повідомлень не зафіксовано.")+"\n\nЦе евристичні попередження, не гарантоване визначення шахрайства.")

@router.message(Command("backups"))
async def cmd_backups(m: Message):
    rows=db.execute("SELECT created_at,size_bytes FROM backup_history WHERE owner_id=? ORDER BY created_at DESC LIMIT 10",(m.from_user.id,)).fetchall()
    await m.answer("💾 <b>Історія резервних копій</b>\n\n"+("\n".join(f"{datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')} — {size:,} байт" for ts,size in rows) if rows else "Копій ще не створювали.")+"\n\n/backup — створити нову копію\n/backup auto — щотижневе копіювання")


# ЗАПУСК
async def main():
    global BOT_USERNAME
    if not TOKEN:
        raise RuntimeError("Не задано BOT_TOKEN у змінних середовища. Додай токен у налаштуваннях хостингу.")
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
