"""VIP Kino Bot — barcha kod bitta faylda (aiogram 3 + SQLite)."""
from __future__ import annotations

import aiosqlite
import asyncio
import hmac
import html
import logging
import os
import re
import secrets
import sqlite3
import time
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, ChatMemberUpdated, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, TelegramObject
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

log = logging.getLogger("bot")

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════
load_dotenv()

TZ = timezone(timedelta(hours=5))  # Toshkent (UTC+5)

# ╔══════════════════════════════════════════════════════════════╗
# ║  SOZLAMALAR — TOKEN VA PAROLNI SHU YERGA YOZING              ║
# ║  (Railway/.env da o'zgaruvchi berilgan bo'lsa, o'sha ustun)  ║
# ╚══════════════════════════════════════════════════════════════╝
BOT_TOKEN = "BU_YERGA_TOKEN_YOZING"      # @BotFather bergan token
ADMIN_COMMAND = "01082008"               # yashirin admin buyrug'i (slashsiz)
ADMIN_PASSWORD = "MJ20080108"            # admin paroli — O'ZGARTIRING!
ADMIN_IDS = ""                           # parolsiz adminlar ID si, masalan "123456,789012" (ixtiyoriy)
DB_PATH = "data/bot.db"                  # baza fayli


def _ids(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit())


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]
    admin_command: str
    admin_password: str
    db_path: str


def _load() -> Config:
    token = (os.getenv("BOT_TOKEN") or BOT_TOKEN).strip()
    if not token or token.startswith("BU_YERGA"):
        raise SystemExit("❌ BOT_TOKEN yozilmagan: main.py tepasidagi BOT_TOKEN ga tokenni yozing.")
    cmd = (os.getenv("ADMIN_COMMAND") or ADMIN_COMMAND).strip().lstrip("/")
    pwd = (os.getenv("ADMIN_PASSWORD") or ADMIN_PASSWORD).strip()
    if not cmd or not pwd:
        raise SystemExit("❌ .env faylida ADMIN_COMMAND va ADMIN_PASSWORD ko'rsatilishi shart.")
    db_path = os.getenv("DB_PATH") or DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    return Config(token, _ids(os.getenv("ADMIN_IDS") or ADMIN_IDS), cmd, pwd, db_path)


config = _load()


# ══════════════════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════════════════
PLANS = {"1m": ("1 OY", 30), "1y": ("1 YIL", 365)}


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=False)


def money(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_dt(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ).strftime("%d.%m.%Y %H:%M")


def fmt_date(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ).strftime("%d.%m.%Y")


def norm(s: str) -> str:
    s = (s or "").lower().strip()
    for ch in "’ʻ‘`´":
        s = s.replace(ch, "'")
    return s


def fmt_card(number: str) -> str:
    d = re.sub(r"\D", "", number)
    return " ".join(d[i:i + 4] for i in range(0, len(d), 4))


def parse_chat_ref(text: str):
    t = (text or "").strip()
    if t.startswith("@") and len(t) > 4:
        return t
    if re.fullmatch(r"-?\d{5,}", t):
        return int(t)
    m = re.match(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", t)
    return "@" + m.group(1) if m else None


def channel_target(link: str):
    m = re.match(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", (link or "").strip())
    return "@" + m.group(1) if m else None


# ══════════════════════════════════════════════════════════════════════
# STATES
# ══════════════════════════════════════════════════════════════════════
class UserStates(StatesGroup):
    code = State()
    search = State()


class PayStates(StatesGroup):
    tg_id = State()
    phone = State()
    amount = State()
    check = State()


class AdminAuth(StatesGroup):
    password = State()


class AddMovie(StatesGroup):
    title = State()
    code = State()
    poster = State()
    video = State()
    vip = State()
    minutes = State()


class DelMovie(StatesGroup):
    code = State()


class EditMovie(StatesGroup):
    code = State()
    value = State()


class AddChannel(StatesGroup):
    ref = State()


class CardStates(StatesGroup):
    number = State()
    owner = State()
    bank = State()


class PriceStates(StatesGroup):
    value = State()


class BlockStates(StatesGroup):
    user_id = State()


class UnblockStates(StatesGroup):
    user_id = State()


class BroadcastStates(StatesGroup):
    content = State()


class SettingStates(StatesGroup):
    value = State()


class GrantStates(StatesGroup):
    user_id = State()
    days = State()
    coins = State()


class StorageStates(StatesGroup):
    ref = State()


class PromoStates(StatesGroup):
    code = State()


class AdOrder(StatesGroup):
    content = State()


class PromoAdmin(StatesGroup):
    code = State()
    amount = State()
    max_uses = State()


class TariffStates(StatesGroup):
    title = State()
    days = State()
    price = State()
    vip_limit = State()
    value = State()


# ══════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════
db_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    first_name TEXT, username TEXT, phone TEXT,
    created_at INTEGER NOT NULL, last_active INTEGER,
    left_bot INTEGER NOT NULL DEFAULT 0,
    views_count INTEGER NOT NULL DEFAULT 0,
    window_start INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    title TEXT NOT NULL, title_lc TEXT NOT NULL,
    poster_id TEXT, video_id TEXT,
    video_type TEXT NOT NULL DEFAULT 'video',
    is_vip INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0,
    src_chat INTEGER,
    src_msg INTEGER,
    teased_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS premium_users(
    user_id INTEGER PRIMARY KEY, plan TEXT,
    activated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    reminded3 INTEGER NOT NULL DEFAULT 0,
    reminded1 INTEGER NOT NULL DEFAULT 0,
    expired_notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, plan TEXT NOT NULL,
    base_amount INTEGER NOT NULL, amount_due INTEGER NOT NULL,
    amount_paid INTEGER,
    status TEXT NOT NULL DEFAULT 'created',
    tg_id_input TEXT, phone TEXT, check_file_id TEXT, check_type TEXT,
    admin_id INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_active_amount
    ON payments(amount_due) WHERE status IN ('created','pending');
CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(status);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE, title TEXT,
    link TEXT NOT NULL, added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS statistics(
    date TEXT PRIMARY KEY,
    views INTEGER NOT NULL DEFAULT 0, new_users INTEGER NOT NULL DEFAULT 0,
    payments INTEGER NOT NULL DEFAULT 0, revenue INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS banned_users(
    user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, banned_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ads(
    id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, kind TEXT,
    src_chat INTEGER, src_msg INTEGER,
    sent INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    action TEXT NOT NULL, details TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites(
    user_id INTEGER NOT NULL, movie_id INTEGER NOT NULL, added_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, movie_id INTEGER NOT NULL, viewed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_history_user ON history(user_id, viewed_at);
CREATE TABLE IF NOT EXISTS tariffs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    days INTEGER NOT NULL, price INTEGER NOT NULL, vip_limit INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS coin_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL, reason TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_codes(
    code TEXT PRIMARY KEY COLLATE NOCASE, kind TEXT NOT NULL, amount INTEGER NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses(
    code TEXT NOT NULL COLLATE NOCASE, user_id INTEGER NOT NULL, used_at INTEGER NOT NULL,
    PRIMARY KEY(code, user_id)
);
CREATE TABLE IF NOT EXISTS scheduled_deletes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, msg_ids TEXT NOT NULL,
    delete_at INTEGER NOT NULL, movie_id INTEGER, done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sched ON scheduled_deletes(done, delete_at);
CREATE TABLE IF NOT EXISTS src_videos(
    src_chat INTEGER NOT NULL, src_msg INTEGER NOT NULL, duration INTEGER,
    PRIMARY KEY(src_chat, src_msg)
);
"""

db_MIGRATIONS = {
    "users": {
        "coins": "INTEGER NOT NULL DEFAULT 0", "referrer_id": "INTEGER",
        "ref_rewarded": "INTEGER NOT NULL DEFAULT 0", "vip_month": "TEXT",
        "vip_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "movies": {"src_chat": "INTEGER", "src_msg": "INTEGER", "teased_at": "INTEGER",
               "duration": "INTEGER", "price": "INTEGER"},
    "premium_users": {"coin_date": "TEXT"},
    "payments": {"qty": "INTEGER NOT NULL DEFAULT 1", "check_unique_id": "TEXT",
                 "src_chat": "INTEGER", "src_msg": "INTEGER"},
}

db_DEFAULTS = {
    "bot_name": "VIP Kino",
    "start_text": (
        "👋 Assalomu alaykum, <b>{name}</b>!\n\n"
        "🎬 <b>{bot}</b> botiga xush kelibsiz.\n"
        "Kino kodini yuboring — kinoni darhol olasiz."
    ),
    "premium_text": "💎 Premium bilan hech qanday cheklovsiz kino tomosha qiling!",
    "ad_text": "",
    "daily_limit": "10",
    "movie_channel": "https://t.me/VIP_kino_ux",
    "card_number": "9860 **** **** ****",
    "card_owner": "Mirzayev Jasur",
    "card_bank": "",
    "price_1m": "8000",
    "price_1y": "29000",
    "reserve_minutes": "60",
    "storage_chat": "",
    "teaser_hour": "18",
    "teaser_last": "",
    "vip_cost": "10",
    "coin_pack_amount": "100",
    "coin_pack_price": "10000",
    "premium_daily_coins": "30",
    "ref_bonus": "10",
    "ad_price": "50000",
    "vip_expire_mult": "6",
    "vip_default_minutes": "120",
}

_db: aiosqlite.Connection
_lock = asyncio.Lock()


# ───────────────────────── yordamchilar ─────────────────────────
def db_now() -> int:
    return int(time.time())


def db_today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def db_day_start() -> int:
    return int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


async def db_init_db() -> None:
    global _db
    _db = await aiosqlite.connect(config.db_path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.executescript(db_SCHEMA)
    for table, wanted in db_MIGRATIONS.items():  # eski va yangi bazalar uchun ustunlar
        cols = {r["name"] for r in await db_fetchall(f"PRAGMA table_info({table})")}
        for col, ddl in wanted.items():
            if col not in cols:
                await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    for k, v in db_DEFAULTS.items():
        await _db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    await _db.commit()
    await _seed_tariffs()


async def _seed_tariffs() -> None:
    if await db_scalar("SELECT COUNT(*) FROM tariffs"):
        return
    for key, title, days, sk in (("1m", "1 OY", 30, "price_1m"), ("1y", "1 YIL", 365, "price_1y")):
        price = int(await db_get_setting(sk))
        await _db.execute(
            "INSERT INTO tariffs(key,title,days,price,vip_limit,active,sort) VALUES(?,?,?,?,0,1,?)",
            (key, title, days, price, days))
    await _db.commit()


async def db_close_db() -> None:
    await _db.close()


async def db_fetchone(sql: str, args=()):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def db_fetchall(sql: str, args=()):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def db_scalar(sql: str, args=()):
    r = await db_fetchone(sql, args)
    return r[0] if r and r[0] is not None else 0


async def db_run(sql: str, args=()):
    cur = await _db.execute(sql, args)
    await _db.commit()
    return cur


# ───────────────────────── sozlamalar / db_log ─────────────────────────
async def db_get_setting(key: str) -> str:
    r = await db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else db_DEFAULTS.get(key, "")


async def db_set_setting(key: str, value: str) -> None:
    await db_run(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def db_log(user_id: int | None, action: str, details: str = "") -> None:
    await db_run(
        "INSERT INTO logs(user_id,action,details,created_at) VALUES(?,?,?,?)",
        (user_id, action, details[:1000], db_now()),
    )


async def db_stat_inc(field: str, amount: int = 1) -> None:
    assert field in ("views", "new_users", "payments", "revenue")
    await db_run(
        f"INSERT INTO statistics(date,{field}) VALUES(?,?) "
        f"ON CONFLICT(date) DO UPDATE SET {field}={field}+excluded.{field}",
        (db_today_key(), amount),
    )


# ───────────────────────── foydalanuvchilar ─────────────────────────
async def db_upsert_user(user_id: int, first_name: str, username: str | None) -> bool:
    t = db_now()
    if await db_fetchone("SELECT 1 FROM users WHERE user_id=?", (user_id,)):
        await db_run(
            "UPDATE users SET first_name=?, username=?, last_active=?, left_bot=0 WHERE user_id=?",
            (first_name, username, t, user_id),
        )
        return False
    await db_run(
        "INSERT INTO users(user_id,first_name,username,created_at,last_active) VALUES(?,?,?,?,?)",
        (user_id, first_name, username, t, t),
    )
    await db_stat_inc("new_users")
    return True


async def db_get_user(user_id: int):
    return await db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def db_set_left(user_id: int, left: bool) -> None:
    await db_run("UPDATE users SET left_bot=? WHERE user_id=?", (1 if left else 0, user_id))


async def db_set_phone(user_id: int, phone: str) -> None:
    await db_run("UPDATE users SET phone=? WHERE user_id=?", (phone, user_id))


async def db_broadcast_user_ids() -> list[int]:
    rows = await db_fetchall(
        "SELECT user_id FROM users WHERE left_bot=0 "
        "AND user_id NOT IN (SELECT user_id FROM banned_users)"
    )
    return [r[0] for r in rows]


# ───────────────────────── kunlik limit ─────────────────────────
async def db_get_usage(user_id: int) -> tuple[int, int]:
    """(ishlatilgan, yangilanish vaqti)"""
    r = await db_fetchone("SELECT views_count, window_start FROM users WHERE user_id=?", (user_id,))
    if r and r["window_start"] and db_now() - r["window_start"] < 86400:
        return r["views_count"], r["window_start"] + 86400
    return 0, 0


async def db_register_view(user_id: int, movie_id: int) -> None:
    t = db_now()
    r = await db_fetchone("SELECT window_start FROM users WHERE user_id=?", (user_id,))
    if r and r["window_start"] and t - r["window_start"] < 86400:
        await db_run("UPDATE users SET views_count=views_count+1 WHERE user_id=?", (user_id,))
    else:
        await db_run("UPDATE users SET views_count=1, window_start=? WHERE user_id=?", (t, user_id))
    await db_run("UPDATE movies SET views=views+1 WHERE id=?", (movie_id,))
    await db_run("INSERT INTO history(user_id,movie_id,viewed_at) VALUES(?,?,?)", (user_id, movie_id, t))
    await db_stat_inc("views")


# ───────────────────────── premium ─────────────────────────
async def db_get_premium(user_id: int):
    return await db_fetchone(
        "SELECT * FROM premium_users WHERE user_id=? AND expires_at>?", (user_id, db_now())
    )


async def db_grant_premium(user_id: int, plan: str, days: int) -> int:
    t = db_now()
    r = await db_fetchone("SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,))
    start = max(t, r["expires_at"]) if r else t
    exp = start + days * 86400
    await db_run(
        "INSERT INTO premium_users(user_id,plan,activated_at,expires_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, activated_at=excluded.activated_at, "
        "expires_at=excluded.expires_at, reminded3=0, reminded1=0, expired_notified=0",
        (user_id, plan, t, exp),
    )
    return exp


async def db_revoke_premium(user_id: int) -> bool:
    cur = await db_run("DELETE FROM premium_users WHERE user_id=?", (user_id,))
    return cur.rowcount > 0


async def db_reminder_candidates():
    return await db_fetchall(
        "SELECT * FROM premium_users WHERE expires_at>? AND (reminded3=0 OR reminded1=0) "
        "AND expires_at-?<=?",
        (db_now(), db_now(), 3 * 86400),
    )


async def db_mark_reminded(user_id: int, level: int) -> None:
    if level == 1:
        await db_run("UPDATE premium_users SET reminded1=1, reminded3=1 WHERE user_id=?", (user_id,))
    else:
        await db_run("UPDATE premium_users SET reminded3=1 WHERE user_id=?", (user_id,))


async def db_expired_unnotified():
    return await db_fetchall(
        "SELECT * FROM premium_users WHERE expires_at<=? AND expired_notified=0", (db_now(),)
    )


async def db_mark_expired_notified(user_id: int) -> None:
    await db_run("UPDATE premium_users SET expired_notified=1 WHERE user_id=?", (user_id,))


# ───────────────────────── kinolar ─────────────────────────
async def db_add_movie(code, title, poster_id, video_id, video_type, is_vip,
                    src_chat=None, src_msg=None, duration=None, price=None) -> int:
    cur = await db_run(
        "INSERT INTO movies(code,title,title_lc,poster_id,video_id,video_type,is_vip,src_chat,src_msg,"
        "duration,price,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, title, norm(title), poster_id, video_id, video_type, int(is_vip), src_chat, src_msg,
         duration, price, db_now()),
    )
    return cur.lastrowid


async def db_get_movie_by_src(src_chat: int, src_msg: int):
    return await db_fetchone("SELECT * FROM movies WHERE src_chat=? AND src_msg=?", (src_chat, src_msg))


async def db_get_movie_by_code(code: str):
    return await db_fetchone("SELECT * FROM movies WHERE code=?", (code.strip(),))


async def db_get_movie(movie_id: int):
    return await db_fetchone("SELECT * FROM movies WHERE id=?", (movie_id,))


async def db_delete_movie(movie_id: int) -> None:
    await _db.execute("DELETE FROM favorites WHERE movie_id=?", (movie_id,))
    await _db.execute("DELETE FROM history WHERE movie_id=?", (movie_id,))
    await _db.execute("DELETE FROM movies WHERE id=?", (movie_id,))
    await _db.commit()


async def db_update_movie(movie_id: int, **fields) -> None:
    allowed = {"title", "code", "poster_id", "video_id", "video_type", "is_vip", "src_chat", "src_msg", "duration", "price"}
    if not fields or not set(fields) <= allowed:
        raise ValueError("noto'g'ri maydon")
    if "title" in fields:
        fields["title_lc"] = norm(fields["title"])
    cols = ", ".join(f"{k}=?" for k in fields)
    await db_run(f"UPDATE movies SET {cols} WHERE id=?", (*fields.values(), movie_id))


async def db_search_movies(query: str, limit: int = 10):
    q = norm(query).replace("%", "").replace("_", "")
    return await db_fetchall(
        "SELECT * FROM movies WHERE title_lc LIKE ? ORDER BY views DESC LIMIT ?",
        (f"%{q}%", limit),
    )


async def db_next_code() -> int:
    r = await db_scalar(
        "SELECT MAX(CAST(code AS INTEGER)) FROM movies WHERE code NOT GLOB '*[^0-9]*'"
    )
    return int(r) + 1 if r else 1001


async def db_pick_vip_for_teaser():
    """Rasmi bor VIP kinolardan eng kam e'lon qilinganini (tasodifiy) tanlaydi."""
    return await db_fetchone(
        "SELECT * FROM movies WHERE is_vip=1 AND poster_id IS NOT NULL "
        "ORDER BY COALESCE(teased_at,0) ASC, RANDOM() LIMIT 1")


async def db_mark_teased(movie_id: int) -> None:
    await db_run("UPDATE movies SET teased_at=? WHERE id=?", (db_now(), movie_id))


async def db_count_movies() -> int:
    return await db_scalar("SELECT COUNT(*) FROM movies")


# ───────────────────────── sevimli / tarix ─────────────────────────
async def db_toggle_favorite(user_id: int, movie_id: int) -> bool:
    if await db_is_favorite(user_id, movie_id):
        await db_run("DELETE FROM favorites WHERE user_id=? AND movie_id=?", (user_id, movie_id))
        return False
    await db_run(
        "INSERT OR IGNORE INTO favorites(user_id,movie_id,added_at) VALUES(?,?,?)",
        (user_id, movie_id, db_now()),
    )
    return True


async def db_is_favorite(user_id: int, movie_id: int) -> bool:
    return bool(await db_fetchone(
        "SELECT 1 FROM favorites WHERE user_id=? AND movie_id=?", (user_id, movie_id)))


async def db_list_favorites(user_id: int, limit: int = 30):
    return await db_fetchall(
        "SELECT m.* FROM favorites f JOIN movies m ON m.id=f.movie_id "
        "WHERE f.user_id=? ORDER BY f.added_at DESC LIMIT ?", (user_id, limit))


async def db_list_history(user_id: int, limit: int = 20):
    return await db_fetchall(
        "SELECT m.*, MAX(h.viewed_at) AS t FROM history h JOIN movies m ON m.id=h.movie_id "
        "WHERE h.user_id=? GROUP BY m.id ORDER BY t DESC LIMIT ?", (user_id, limit))


# ───────────────────────── majburiy kanallar ─────────────────────────
async def db_add_channel(chat_id: int, title: str, link: str) -> None:
    await db_run(
        "INSERT INTO channels(chat_id,title,link,added_at) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, link=excluded.link",
        (chat_id, title, link, db_now()),
    )


async def db_list_channels():
    return await db_fetchall("SELECT * FROM channels ORDER BY id")


async def db_get_channel(cid: int):
    return await db_fetchone("SELECT * FROM channels WHERE id=?", (cid,))


async def db_delete_channel(cid: int) -> None:
    await db_run("DELETE FROM channels WHERE id=?", (cid,))


# ───────────────────────── adminlar / bloklash ─────────────────────────
async def db_add_admin(user_id: int) -> None:
    await db_run("INSERT OR IGNORE INTO admins(user_id,added_at) VALUES(?,?)", (user_id, db_now()))


async def db_remove_admin(user_id: int) -> None:
    await db_run("DELETE FROM admins WHERE user_id=?", (user_id,))


async def db_is_admin(user_id: int) -> bool:
    if user_id in config.admin_ids:
        return True
    return bool(await db_fetchone("SELECT 1 FROM admins WHERE user_id=?", (user_id,)))


async def db_admin_ids() -> set[int]:
    rows = await db_fetchall("SELECT user_id FROM admins")
    return {r[0] for r in rows} | set(config.admin_ids)


async def db_ban_user(user_id: int, by: int, reason: str = "") -> None:
    await db_run(
        "INSERT OR REPLACE INTO banned_users(user_id,reason,banned_by,banned_at) VALUES(?,?,?,?)",
        (user_id, reason, by, db_now()),
    )


async def db_unban_user(user_id: int) -> bool:
    cur = await db_run("DELETE FROM banned_users WHERE user_id=?", (user_id,))
    return cur.rowcount > 0


async def db_is_banned(user_id: int) -> bool:
    return bool(await db_fetchone("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)))


# ───────────────────────── to'lovlar ─────────────────────────
async def db_expire_stale_payments() -> None:
    minutes = int(await db_get_setting("reserve_minutes") or 60)
    await db_run(
        "UPDATE payments SET status='expired', updated_at=? WHERE status='created' AND updated_at<?",
        (db_now(), db_now() - minutes * 60),
    )


async def db_reserve_payment(user_id: int, plan: str, base: int, qty: int = 1,
                          src_chat: int | None = None, src_msg: int | None = None):
    """Foydalanuvchiga noyob summa ajratadi (base+1, base+2, ...)."""
    async with _lock:
        await db_expire_stale_payments()
        await _db.execute(
            "UPDATE payments SET status='cancelled', updated_at=? WHERE user_id=? AND status='created'",
            (db_now(), user_id),
        )
        await _db.commit()
        rows = await db_fetchall("SELECT amount_due FROM payments WHERE status IN ('created','pending')")
        used = {r[0] for r in rows}
        for n in range(1, 1000):
            if base + n not in used:
                amount = base + n
                break
        else:
            return None
        t = db_now()
        cur = await db_run(
            "INSERT INTO payments(user_id,plan,base_amount,amount_due,status,created_at,updated_at,"
            "qty,src_chat,src_msg) VALUES(?,?,?,?,'created',?,?,?,?,?)",
            (user_id, plan, base, amount, t, t, qty, src_chat, src_msg),
        )
        return await db_get_payment(cur.lastrowid)


async def db_get_payment(pid: int):
    return await db_fetchone("SELECT * FROM payments WHERE id=?", (pid,))


async def db_get_pending_for_user(user_id: int):
    return await db_fetchone(
        "SELECT * FROM payments WHERE user_id=? AND status='pending'", (user_id,))


async def db_touch_payment(pid: int) -> None:
    await db_run("UPDATE payments SET updated_at=? WHERE id=? AND status='created'", (db_now(), pid))


async def db_cancel_payment(pid: int, user_id: int) -> None:
    await db_run(
        "UPDATE payments SET status='cancelled', updated_at=? WHERE id=? AND user_id=? AND status='created'",
        (db_now(), pid, user_id),
    )


async def db_submit_payment(pid, tg_id_input, phone, amount_paid, check_file_id, check_type,
                         unique_id=None) -> bool:
    cur = await db_run(
        "UPDATE payments SET status='pending', tg_id_input=?, phone=?, amount_paid=?, "
        "check_file_id=?, check_type=?, check_unique_id=?, updated_at=? WHERE id=? AND status='created'",
        (tg_id_input, phone, amount_paid, check_file_id, check_type, unique_id, db_now(), pid),
    )
    return cur.rowcount > 0


async def db_find_payment_by_unique(unique_id: str):
    return await db_fetchone(
        "SELECT * FROM payments WHERE check_unique_id=? AND status IN ('pending','approved','rejected') LIMIT 1",
        (unique_id,))


async def db_approve_payment(pid: int, admin_id: int) -> bool:
    cur = await db_run(
        "UPDATE payments SET status='approved', admin_id=?, updated_at=? WHERE id=? AND status='pending'",
        (admin_id, db_now(), pid),
    )
    return cur.rowcount > 0


async def db_reject_payment(pid: int, admin_id: int) -> bool:
    cur = await db_run(
        "UPDATE payments SET status='rejected', admin_id=?, updated_at=? WHERE id=? AND status='pending'",
        (admin_id, db_now(), pid),
    )
    return cur.rowcount > 0


async def db_list_pending(limit: int = 20):
    return await db_fetchall(
        "SELECT * FROM payments WHERE status='pending' ORDER BY id LIMIT ?", (limit,))


# ───────────────────────── reklama / statistika ─────────────────────────
async def db_add_ad(admin_id: int, kind: str, src_chat: int, src_msg: int) -> int:
    cur = await db_run(
        "INSERT INTO ads(admin_id,kind,src_chat,src_msg,created_at) VALUES(?,?,?,?,?)",
        (admin_id, kind, src_chat, src_msg, db_now()),
    )
    return cur.lastrowid


async def db_finish_ad(ad_id: int, sent: int, failed: int) -> None:
    await db_run("UPDATE ads SET sent=?, failed=? WHERE id=?", (sent, failed, ad_id))


async def db_stats_summary() -> dict:
    t = db_now()
    total = await db_scalar("SELECT COUNT(*) FROM users")
    premium = await db_scalar("SELECT COUNT(*) FROM premium_users WHERE expires_at>?", (t,))
    today = await db_fetchone(
        "SELECT views, payments, revenue FROM statistics WHERE date=?", (db_today_key(),))
    return {
        "total": total,
        "new_today": await db_scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", (db_day_start(),)),
        "premium": premium,
        "regular": max(total - premium, 0),
        "banned": await db_scalar("SELECT COUNT(*) FROM banned_users"),
        "left": await db_scalar("SELECT COUNT(*) FROM users WHERE left_bot=1"),
        "pay_today": today["payments"] if today else 0,
        "rev_today": today["revenue"] if today else 0,
        "revenue": await db_scalar("SELECT COALESCE(SUM(revenue),0) FROM statistics"),
        "views_today": today["views"] if today else 0,
        "movies": await db_count_movies(),
    }


# ───────────────────────── tariflar ─────────────────────────
async def db_list_tariffs(active_only: bool = True):
    where = " WHERE active=1" if active_only else ""
    return await db_fetchall(f"SELECT * FROM tariffs{where} ORDER BY days, id")


async def db_get_tariff(tid: int):
    return await db_fetchone("SELECT * FROM tariffs WHERE id=?", (tid,))


async def db_get_tariff_by_key(key: str):
    return await db_fetchone("SELECT * FROM tariffs WHERE key=?", (key,))


async def db_add_tariff(title: str, days: int, price: int, vip_limit: int = 0) -> int:
    cur = await db_run(
        "INSERT INTO tariffs(key,title,days,price,vip_limit,active,sort) VALUES(?,?,?,?,?,1,?)",
        (f"tmp{time.time_ns()}", title, days, price, vip_limit, days))
    tid = cur.lastrowid
    await db_run("UPDATE tariffs SET key=? WHERE id=?", (f"t{tid}", tid))
    return tid


async def db_update_tariff(tid: int, **fields) -> None:
    allowed = {"title", "days", "price", "vip_limit", "active"}
    if not fields or not set(fields) <= allowed:
        raise ValueError("noto'g'ri maydon")
    cols = ", ".join(f"{k}=?" for k in fields)
    await db_run(f"UPDATE tariffs SET {cols} WHERE id=?", (*fields.values(), tid))


# ───────────────────────── coin ─────────────────────────
async def db_get_coins(user_id: int) -> int:
    return int(await db_scalar("SELECT coins FROM users WHERE user_id=?", (user_id,)))


async def db_add_coins(user_id: int, n: int, reason: str = "") -> bool:
    cur = await _db.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (n, user_id))
    ok = cur.rowcount > 0
    if ok:
        await _db.execute("INSERT INTO coin_log(user_id,delta,reason,created_at) VALUES(?,?,?,?)",
                          (user_id, n, reason, db_now()))
    await _db.commit()
    return ok


async def db_spend_coins(user_id: int, n: int, reason: str = "") -> bool:
    """Atomik: balans yetarli bo'lsagina yechadi."""
    cur = await _db.execute("UPDATE users SET coins=coins-? WHERE user_id=? AND coins>=?",
                            (n, user_id, n))
    ok = cur.rowcount > 0
    if ok:
        await _db.execute("INSERT INTO coin_log(user_id,delta,reason,created_at) VALUES(?,?,?,?)",
                          (user_id, -n, reason, db_now()))
    await _db.commit()
    return ok


async def db_premium_coin_candidates(date_key: str):
    return await db_fetchall(
        "SELECT user_id FROM premium_users WHERE expires_at>? AND COALESCE(coin_date,'')!=?",
        (db_now(), date_key))


async def db_mark_coin_date(user_id: int, date_key: str) -> None:
    await db_run("UPDATE premium_users SET coin_date=? WHERE user_id=?", (date_key, user_id))


# ───────────────────────── referal ─────────────────────────
async def db_set_referrer(user_id: int, ref_id: int) -> None:
    if ref_id == user_id or not await db_get_user(ref_id):
        return
    await db_run("UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL", (ref_id, user_id))


async def db_get_pending_referrer(user_id: int):
    r = await db_fetchone(
        "SELECT referrer_id FROM users WHERE user_id=? AND referrer_id IS NOT NULL AND ref_rewarded=0",
        (user_id,))
    return r[0] if r else None


async def db_mark_ref_rewarded(user_id: int) -> None:
    await db_run("UPDATE users SET ref_rewarded=1 WHERE user_id=?", (user_id,))


async def db_count_referrals(ref_id: int) -> int:
    return await db_scalar("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_rewarded=1", (ref_id,))


# ───────────────────────── VIP oylik limit ─────────────────────────
def db_month_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m")


async def db_vip_used_this_month(user_id: int) -> int:
    r = await db_fetchone("SELECT vip_month, vip_count FROM users WHERE user_id=?", (user_id,))
    return r["vip_count"] if r and r["vip_month"] == db_month_key() else 0


async def db_vip_inc(user_id: int) -> None:
    if await db_vip_used_this_month(user_id):
        await db_run("UPDATE users SET vip_count=vip_count+1 WHERE user_id=?", (user_id,))
    else:
        await db_run("UPDATE users SET vip_month=?, vip_count=1 WHERE user_id=?", (db_month_key(), user_id))


# ───────────────────────── promokodlar ─────────────────────────
async def db_create_promo(code: str, kind: str, amount: int, max_uses: int) -> bool:
    try:
        await db_run("INSERT INTO promo_codes(code,kind,amount,max_uses,created_at) VALUES(?,?,?,?,?)",
                  (code, kind, amount, max_uses, db_now()))
        return True
    except sqlite3.IntegrityError:
        return False


async def db_get_promo(code: str):
    return await db_fetchone("SELECT * FROM promo_codes WHERE code=?", (code,))


async def db_list_promos(limit: int = 30):
    return await db_fetchall("SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT ?", (limit,))


async def db_delete_promo(code: str) -> None:
    await db_run("DELETE FROM promo_codes WHERE code=?", (code,))


async def db_redeem_promo(code: str, user_id: int):
    """('ok'|'none'|'used'|'exhausted', promo)"""
    async with _lock:
        p = await db_get_promo(code)
        if not p:
            return "none", None
        if await db_fetchone("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, user_id)):
            return "used", p
        if p["max_uses"] and p["used"] >= p["max_uses"]:
            return "exhausted", p
        await _db.execute("INSERT INTO promo_uses(code,user_id,used_at) VALUES(?,?,?)",
                          (code, user_id, db_now()))
        await _db.execute("UPDATE promo_codes SET used=used+1 WHERE code=?", (code,))
        await _db.commit()
        return "ok", p


# ───────────────────────── rejalashtirilgan o'chirishlar ─────────────────────────
async def db_schedule_delete(user_id: int, msg_ids: list[int], delete_at: int, movie_id: int | None) -> None:
    await db_run("INSERT INTO scheduled_deletes(user_id,msg_ids,delete_at,movie_id) VALUES(?,?,?,?)",
              (user_id, ",".join(str(i) for i in msg_ids), delete_at, movie_id))


async def db_due_deletes(limit: int = 50):
    return await db_fetchall(
        "SELECT * FROM scheduled_deletes WHERE done=0 AND delete_at<=? ORDER BY delete_at LIMIT ?",
        (db_now(), limit))


async def db_done_delete(did: int) -> None:
    await db_run("UPDATE scheduled_deletes SET done=1 WHERE id=?", (did,))


# ───────────────────────── yopiq kanal videolari ─────────────────────────
async def db_save_src_video(src_chat: int, src_msg: int, duration: int | None) -> None:
    await db_run("INSERT OR REPLACE INTO src_videos(src_chat,src_msg,duration) VALUES(?,?,?)",
              (src_chat, src_msg, duration))


async def db_get_src_video(src_chat: int, src_msg: int):
    return await db_fetchone("SELECT * FROM src_videos WHERE src_chat=? AND src_msg=?", (src_chat, src_msg))


def db_plan_days(plan: str) -> int:
    return PLANS[plan][1]


# ══════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════
# ── foydalanuvchi tugmalari (matnlar o'zgarmaydi — handlerlar shu matnga bog'langan)
kb_BTN_MOVIE = "🎬 Kino ko'rish"
kb_BTN_PREMIUM = "⭐ Premium"
kb_BTN_CHANNEL = "📢 Kino kanal"
kb_BTN_ADS = "📣 Reklama"
kb_BTN_PROFILE = "👤 Profil"
kb_BTN_FAVS = "❤️ Sevimlilar"
kb_BTN_SEARCH = "🔎 Qidirish"
kb_BTN_HISTORY = "📜 Tarix"
kb_BTN_CANCEL = "🔙 Bekor qilish"
kb_BTN_MY_ID = "🆔 Mening ID raqamim"
kb_BTN_SEND_PHONE = "📱 Raqamimni yuborish"
kb_BTN_COIN = "🪙 Coin"
kb_BTN_REF = "👥 Referal"
kb_BTN_PROMO = "🎟 Promokod"

# ── admin tugmalari
kb_A_ADD_MOVIE = "➕ Kino qo'shish"
kb_A_DEL_MOVIE = "🗑 Kino o'chirish"
kb_A_EDIT_MOVIE = "✏ Kino tahrirlash"
kb_A_ADD_CHANNEL = "📢 Majburiy kanal qo'shish"
kb_A_DEL_CHANNEL = "🗑 Majburiy kanal o'chirish"
kb_A_CARD = "💳 Kartani almashtirish"
kb_A_PRICE = "🏷 Tariflar"
kb_A_PROMO = "🎟 Promokodlar"
kb_A_PAYMENTS = "💰 Premium to'lovlari"
kb_A_BROADCAST = "📨 Reklama yuborish"
kb_A_BAN = "🚫 Foydalanuvchini bloklash"
kb_A_UNBAN = "✅ Blokdan chiqarish"
kb_A_STATS = "📊 Statistika"
kb_A_GRANT = "🎁 Premium berish"
kb_A_STORAGE = "📦 Video kanal"
kb_A_SETTINGS = "⚙ Sozlamalar"
kb_A_EXIT = "🚪 Chiqish"

# Tugma ranglari (Telegram Bot API 9.4+): primary=ko'k, success=yashil, danger=qizil
P, S, D = "primary", "success", "danger"


def _rb(text: str, style: str | None = None, **kw) -> KeyboardButton:
    return KeyboardButton(text=text, style=style, **kw)


def _ib(text: str, data: str | None = None, style: str | None = None,
        url: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url, style=style)
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _rk(rows: list[list[KeyboardButton]], placeholder: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               input_field_placeholder=placeholder)


def _ik(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═════════════════ reply klaviaturalar ═════════════════
def kb_main_menu() -> ReplyKeyboardMarkup:
    return _rk([
        [_rb(kb_BTN_MOVIE, P)],
        [_rb(kb_BTN_SEARCH), _rb(kb_BTN_PROFILE)],
        [_rb(kb_BTN_PREMIUM, S), _rb(kb_BTN_COIN, S)],
        [_rb(kb_BTN_FAVS), _rb(kb_BTN_HISTORY)],
        [_rb(kb_BTN_REF), _rb(kb_BTN_PROMO)],
        [_rb(kb_BTN_CHANNEL), _rb(kb_BTN_ADS)],
    ], placeholder="🎬 Kino kodi yoki nomini yozing…")


def kb_cancel_kb() -> ReplyKeyboardMarkup:
    return _rk([[_rb(kb_BTN_CANCEL, D)]])


def kb_tgid_kb() -> ReplyKeyboardMarkup:
    return _rk([[_rb(kb_BTN_MY_ID, S)], [_rb(kb_BTN_CANCEL, D)]], placeholder="Telegram ID raqami…")


def kb_phone_kb() -> ReplyKeyboardMarkup:
    return _rk([[_rb(kb_BTN_SEND_PHONE, S, request_contact=True)], [_rb(kb_BTN_CANCEL, D)]],
               placeholder="+998901234567")


def kb_admin_menu() -> ReplyKeyboardMarkup:
    return _rk([
        [_rb(kb_A_ADD_MOVIE, S), _rb(kb_A_DEL_MOVIE, D)],
        [_rb(kb_A_EDIT_MOVIE, P), _rb(kb_A_STORAGE, P)],
        [_rb(kb_A_ADD_CHANNEL, S), _rb(kb_A_DEL_CHANNEL, D)],
        [_rb(kb_A_PAYMENTS, P), _rb(kb_A_GRANT, S)],
        [_rb(kb_A_PRICE, P), _rb(kb_A_PROMO, P)],
        [_rb(kb_A_CARD), _rb(kb_A_BROADCAST, P)],
        [_rb(kb_A_STATS, P), _rb(kb_A_SETTINGS)],
        [_rb(kb_A_BAN, D), _rb(kb_A_UNBAN, S)],
        [_rb(kb_A_EXIT, D)],
    ], placeholder="🛠 Admin panel")


# ═════════════════ inline klaviaturalar ═════════════════
def kb_subscribe_kb(channels, missing_ids: set | None = None) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        joined = missing_ids is not None and ch["id"] not in missing_ids
        rows.append([_ib(f"{'✅' if joined else '📢'} {ch['title'] or 'Kanal'}",
                         url=ch["link"], style=S if joined else P)])
    rows.append([_ib("🔄 Tekshirish", "check_sub", S)])
    rows.append([_ib("⭐ Premium sotib olish", "premium")])
    return _ik(rows)


def kb_premium_kb(tariffs) -> InlineKeyboardMarkup:
    ppd = {t["id"]: t["price"] / max(t["days"], 1) for t in tariffs}
    best = min(ppd, key=ppd.get) if len(tariffs) > 1 else None
    worst = max(ppd.values()) if ppd else 0
    rows = []
    for t in tariffs:
        is_best = best is not None and t["id"] == best
        pct = round(100 * (1 - ppd[t["id"]] / worst)) if is_best and worst > 0 else 0
        label = f"{'🔥' if is_best else '📅'} {t['title']} — {money(t['price'])} so'm"
        if pct:
            label += f" • -{pct}%"
        rows.append([_ib(label, f"plan:{t['key']}", S if is_best else P)])
    return _ik(rows)


def kb_coin_kb(amount: int, price: int) -> InlineKeyboardMarkup:
    rows = []
    for q in (1, 3, 5, 10):
        rows.append([_ib(f"🪙 {amount * q} coin — {money(price * q)} so'm", f"coinbuy:{q}",
                         P if q == 1 else S)])
    return _ik(rows)


def kb_premium_btn() -> InlineKeyboardMarkup:
    return _ik([[_ib("⭐ Premium sotib olish", "premium", S)]])


def kb_pay_kb(pid: int) -> InlineKeyboardMarkup:
    return _ik([[_ib("💸 Pul tashladim", f"pay:{pid}", S)],
                [_ib("❌ Bekor qilish", f"paycancel:{pid}", D)]])


def kb_movie_kb(movie_id: int, is_fav: bool, url: str | None = None,
             upsell: bool = False) -> InlineKeyboardMarkup:
    rows = [[_ib("💔 Sevimlidan olib tashlash" if is_fav else "❤️ Sevimlilarga qo'shish",
                 f"fav:{movie_id}", D if is_fav else None)]]
    if url:
        rows.insert(0, [_ib("▶️ Ko'rish", url=url, style=P)])
    if upsell:
        rows.append([_ib("⭐ Cheksiz ko'rish — Premium", "premium", S)])
    return _ik(rows)


def kb_movies_list_kb(movies) -> InlineKeyboardMarkup:
    rows = []
    for m in movies:
        icon = "💎" if m["is_vip"] else "🎬"
        rows.append([_ib(f"{icon} {m['title']} • {m['code']}"[:60], f"mv:{m['code']}",
                         S if m["is_vip"] else None)])
    return _ik(rows)


def kb_pay_admin_kb(pid: int) -> InlineKeyboardMarkup:
    return _ik([[_ib("✅ Tasdiqlash", f"pa:ok:{pid}", S), _ib("❌ Bekor qilish", f"pa:no:{pid}", D)]])


def kb_vip_kb() -> InlineKeyboardMarkup:
    return _ik([[_ib("🆓 Oddiy", "vip:0", P), _ib("💎 VIP", "vip:1", S)]])


def kb_chpost_kb(movie_id: int) -> InlineKeyboardMarkup:
    return _ik([[_ib("✅ Kanalga yuborish", f"chpost:ok:{movie_id}", S),
                 _ib("❌ Yubormaslik", f"chpost:no:{movie_id}", D)]])


def kb_channel_post_kb(url: str) -> InlineKeyboardMarkup:
    return _ik([[_ib("🎬 Botda ko'rish", url=url, style=P)]])


def kb_confirm_delete_kb(movie_id: int) -> InlineKeyboardMarkup:
    return _ik([[_ib("🗑 Ha, o'chirish", f"del:ok:{movie_id}", D), _ib("↩️ Yo'q", "del:no:0")]])


def kb_edit_kb(movie_id: int, is_vip: bool) -> InlineKeyboardMarkup:
    return _ik([
        [_ib("📝 Nomi", f"ed:title:{movie_id}"), _ib("🔑 Kodi", f"ed:code:{movie_id}")],
        [_ib("🖼 Rasmi", f"ed:poster:{movie_id}"), _ib("🎞 Videosi", f"ed:video:{movie_id}")],
        [_ib("⏱ Davomiyligi", f"ed:minutes:{movie_id}"), _ib("💰 Narxi (coin)", f"ed:price:{movie_id}")],
        [_ib("🆓 Oddiy qilish" if is_vip else "💎 VIP qilish", f"ed:vip:{movie_id}", P)],
    ])


def kb_channels_del_kb(channels) -> InlineKeyboardMarkup:
    return _ik([[_ib(f"🗑 {ch['title'] or ch['chat_id']}"[:60], f"chdel:{ch['id']}", D)]
                for ch in channels])


def kb_pay_force_kb(pid: int) -> InlineKeyboardMarkup:
    return _ik([[_ib("✅ Chekda summa to'g'ri — tasdiqlash", f"pa:force:{pid}", S)],
                [_ib("❌ Bekor qilish", f"pa:no:{pid}", D)]])


def kb_tariffs_admin_kb(tariffs) -> InlineKeyboardMarkup:
    rows = [[_ib(f"✏ {t['title']} • {t['days']} kun • {money(t['price'])}"
                 + (f" • VIP {t['vip_limit']}/oy" if t["vip_limit"] else ""), f"tf:edit:{t['id']}")]
            for t in tariffs]
    rows.append([_ib("➕ Yangi tarif", "tf:new", S)])
    return _ik(rows)


def kb_tariff_edit_kb(tid: int) -> InlineKeyboardMarkup:
    return _ik([
        [_ib("💰 Narx", f"tf:f:price:{tid}", P), _ib("📅 Muddat", f"tf:f:days:{tid}", P)],
        [_ib("📝 Nomi", f"tf:f:title:{tid}"), _ib("👑 VIP oylik limit", f"tf:f:vip_limit:{tid}")],
        [_ib("🗑 O'chirish", f"tf:del:{tid}", D), _ib("↩️ Orqaga", "tf:list")],
    ])


def kb_promos_kb(promos) -> InlineKeyboardMarkup:
    rows = []
    for p in promos:
        what = f"🪙{p['amount']}" if p["kind"] == "coin" else f"⭐{p['amount']}k"
        lim = p["max_uses"] or "∞"
        rows.append([_ib(f"🗑 {p['code']} • {what} • {p['used']}/{lim}", f"pc:del:{p['code']}", D)])
    rows.append([_ib("➕ Yangi promokod", "pc:new", S)])
    return _ik(rows)


def kb_promo_kind_kb() -> InlineKeyboardMarkup:
    return _ik([[_ib("🪙 Coin", "pc:kind:coin", P), _ib("⭐ Premium (kun)", "pc:kind:premium", S)]])


def kb_vip_confirm_kb(code: str, cost: int) -> InlineKeyboardMarkup:
    return _ik([[_ib(f"✅ Ko'rish — {cost} coin", f"vb:{code}", S)],
                [_ib("↩️ Bekor qilish", "vbno", D)]])


def kb_vip_nocoin_kb() -> InlineKeyboardMarkup:
    return _ik([[_ib("🪙 Coin sotib olish", "coin", S)], [_ib("⭐ Premium", "premium", P)]])


def kb_vip_expired_kb(code: str, cost: int) -> InlineKeyboardMarkup:
    return _ik([[_ib(f"🔄 Yana sotib olish — {cost} coin", f"vb:{code}", S)]])


def kb_ad_page_kb() -> InlineKeyboardMarkup:
    return _ik([[_ib("📣 Reklama berish", "adbuy", S)]])


def kb_share_kb(url: str) -> InlineKeyboardMarkup:
    return _ik([[_ib("📤 Do'stlarga yuborish", url=url, style=P)]])


def kb_grant_kb(uid: int, tariffs) -> InlineKeyboardMarkup:
    rows = [[_ib(f"📅 {t['title']} ({t['days']} kun)", f"gr:{t['key']}:{uid}", S)] for t in tariffs]
    rows.append([_ib("✏ Kun kiritish", f"gr:days:{uid}", P), _ib("🪙 Coin berish", f"gr:coin:{uid}", P)])
    rows.append([_ib("🗑 Premiumni olib tashlash", f"gr:rev:{uid}", D)])
    return _ik(rows)


def kb_storage_kb(msg_id: int) -> InlineKeyboardMarkup:
    return _ik([[_ib("➕ Kino qo'shish", f"sv:ok:{msg_id}", S),
                 _ib("⏭ O'tkazib yuborish", f"sv:no:{msg_id}")]])


def kb_broadcast_kb() -> InlineKeyboardMarkup:
    return _ik([[_ib("🚀 Yuborish", "bc:ok", S), _ib("❌ Bekor qilish", "bc:no", D)]])


def kb_teaser_kb(code: str, premium: bool, cost: int) -> InlineKeyboardMarkup:
    rows = [[_ib(f"🎬 Ko'rish — {cost} coin", f"mv:{code}", P)]]
    if not premium:
        rows.append([_ib("⭐ Premium — har kuni coin", "premium", S)])
    return _ik(rows)


def kb_settings_kb(editable: dict[str, str]) -> InlineKeyboardMarkup:
    rows = [[_ib(label, f"st:{key}")] for key, label in editable.items()]
    rows.append([_ib("💳 Karta ma'lumotlari", "st:card", P), _ib("🏷 Tariflar", "st:prices", P)])
    rows.append([_ib("🔗 Majburiy kanallar", "st:channels")])
    rows.append([_ib("👁 VIP postni sinash", "st:teaser_test", S)])
    return _ik(rows)


# ══════════════════════════════════════════════════════════════════════
# SERVICES
# ══════════════════════════════════════════════════════════════════════
svc_log = logging.getLogger(__name__)
_ok_cache: dict[int, float] = {}
svc_CACHE_TTL = 45


async def svc_not_joined(bot: Bot, user_id: int, force: bool = False) -> list:
    """Foydalanuvchi a'zo bo'lmagan majburiy kanallar ro'yxati."""
    channels = await db_list_channels()
    if not channels:
        return []
    if not force and time.time() - _ok_cache.get(user_id, 0) < svc_CACHE_TTL:
        return []

    async def check(ch):
        try:
            m = await bot.get_chat_member(ch["chat_id"], user_id)
            if m.status in ("left", "kicked"):
                return ch
            if m.status == "restricted" and not getattr(m, "is_member", True):
                return ch
        except TelegramAPIError as e:  # bot kanalda admin emas / kanal yo'q — foydalanuvchini bloklamaymiz
            svc_log.warning("Kanal %s tekshirib bo'lmadi: %s", ch["chat_id"], e)
        return None

    res = await asyncio.gather(*(check(c) for c in channels))
    missing = [r for r in res if r]
    if not missing:
        _ok_cache[user_id] = time.time()
        if len(_ok_cache) > 20000:
            _ok_cache.clear()
    else:
        _ok_cache.pop(user_id, None)
    return missing


async def svc_notify_admins(bot: Bot, text: str, **kw) -> None:
    for aid in await db_admin_ids():
        try:
            await bot.send_message(aid, text, **kw)
        except TelegramAPIError:
            pass


async def svc_plan_title(p) -> str:
    plan = p["plan"]
    if plan == "coin":
        amount = int(await db_get_setting("coin_pack_amount") or 100)
        return f"🪙 Coin — {amount * (p['qty'] or 1)} ta"
    if plan == "ad":
        return "📣 Reklama joylash"
    t = await db_get_tariff_by_key(plan)
    return f"⭐ Premium — {t['title']}" if t else plan


async def svc_payment_text(p) -> str:
    u = await db_get_user(p["user_id"])
    name = esc(u["first_name"]) if u else "—"
    uname = f"@{esc(u['username'])}" if u and u["username"] else "—"
    paid = p["amount_paid"]
    warn = "" if paid == p["amount_due"] else "  ⚠️ <b>MOS EMAS!</b>"
    card = await db_get_setting("card_number")
    return (
        "━━━━━━━━━━━━━━\n"
        "🆕 <b>PREMIUM SO'ROVI</b>\n\n"
        f"👤 Ism: {name}\n"
        f"🆔 Telegram ID: <code>{p['user_id']}</code>\n"
        f"🆔 Kiritilgan ID: <code>{esc(p['tg_id_input'])}</code>\n"
        f"👤 Username: {uname}\n"
        f"📱 Telefon: <code>{esc(p['phone'])}</code>\n"
        f"📦 Xarid: {await svc_plan_title(p)}\n"
        f"💰 Kerakli summa: <b>{money(p['amount_due'])}</b> so'm\n"
        f"💵 To'langan summa: <b>{money(paid) if paid is not None else '—'}</b> so'm{warn}\n"
        f"💳 Karta: <code>{esc(card)}</code>\n"
        f"🕒 Sana: {fmt_dt(p['updated_at'])}\n"
        f"📷 Chek: {'✅ ilova qilingan' if p['check_file_id'] else '❌ yo`q'}\n"
        "━━━━━━━━━━━━━━"
    )


async def svc_send_payment_to(bot: Bot, chat_id: int, p) -> None:
    text = await svc_payment_text(p)
    markup = kb_pay_admin_kb(p["id"])
    try:
        if p["check_type"] == "document":
            await bot.send_document(chat_id, p["check_file_id"], caption=text, reply_markup=markup)
        else:
            await bot.send_photo(chat_id, p["check_file_id"], caption=text, reply_markup=markup)
        if p["plan"] == "ad" and p["src_msg"]:  # reklama matnini ham ko'rsatamiz
            await bot.send_message(chat_id, "📣 <b>Yuqoridagi to'lov uchun reklama:</b>")
            await bot.copy_message(chat_id, p["src_chat"], p["src_msg"])
    except TelegramAPIError as e:
        svc_log.warning("To'lov adminga yuborilmadi (%s): %s", chat_id, e)


async def svc_notify_admins_payment(bot: Bot, pid: int) -> None:
    p = await db_get_payment(pid)
    for aid in await db_admin_ids():
        await svc_send_payment_to(bot, aid, p)


def svc_channel_caption(movie, bot_username: str) -> str:
    vip = "💎 <b>VIP</b>\n" if movie["is_vip"] else ""
    return (
        f"🎬 <b>{esc(movie['title'])}</b>\n{vip}\n"
        f"🔑 Kino kodi: <code>{esc(movie['code'])}</code>\n\n"
        f"🤖 @{bot_username} botiga kodni yuboring"
    )


def svc_card_block(card: str, owner: str, bank: str) -> str:
    s = f"💳 Karta: <code>{esc(fmt_card(card) if card.replace(' ', '').isdigit() else card)}</code>\n"
    s += f"👤 Karta egasi: <b>{esc(owner)}</b>\n"
    if bank:
        s += f"🏦 Bank: {esc(bank)}\n"
    return s


# ══════════════════════════════════════════════════════════════════════
# MIDDLEWARES
# ══════════════════════════════════════════════════════════════════════
class AccessMiddleware(BaseMiddleware):
    """Ro'yxatga olish, antispam va bloklangan foydalanuvchilarni to'xtatish."""

    def __init__(self) -> None:
        self._last: dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or user.is_bot:
            return await handler(event, data)
        if chat is not None and chat.type != "private":
            return None
        is_new = await db_upsert_user(user.id, user.first_name or "", user.username)
        if is_new and isinstance(event, Message) and (event.text or "").startswith("/start r_"):
            arg = event.text.split("r_", 1)[1].strip()
            if arg.isdigit():  # referal havola orqali kelgan
                await db_set_referrer(user.id, int(arg))
        if await db_is_admin(user.id):
            return await handler(event, data)

        if isinstance(event, Message):
            t = time.monotonic()
            if t - self._last.get(user.id, 0) < 0.3:
                return None
            self._last[user.id] = t
            if len(self._last) > 20000:
                self._last.clear()

        if await db_is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 Siz bloklangansiz.")
            return None
        return await handler(event, data)


class SubscriptionGate(BaseMiddleware):
    """Majburiy obuna: Premium va adminlar bundan ozod."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if await db_is_admin(user.id) or await db_get_premium(user.id):
            return await handler(event, data)

        state: FSMContext = data["state"]
        bot: Bot = data["bot"]

        # Obuna bo'lmagan odam ham Premium sotib ola olishi kerak
        if isinstance(event, CallbackQuery):
            d = event.data or ""
            if d in ("check_sub", "premium", "coin") or d.startswith(
                    ("plan:", "pay:", "paycancel:", "coinbuy:")):
                return await handler(event, data)
        else:
            cur = await state.get_state()
            if cur and cur.startswith("PayStates"):
                return await handler(event, data)

        missing = await svc_not_joined(bot, user.id)
        if not missing:
            return await handler(event, data)

        if isinstance(event, Message) and (event.text or "").startswith("/start m_"):
            await state.update_data(pending_code=event.text.split("m_", 1)[1].strip())

        if isinstance(event, CallbackQuery):
            await event.answer()
        channels = await db_list_channels()
        await bot.send_message(
            user.id,
            "📢 <b>Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling</b>\n\n"
            "So'ng «🔄 Tekshirish» tugmasini bosing.\n"
            "⭐ Premium foydalanuvchilar majburiy obunasiz ishlaydi.",
            reply_markup=kb_subscribe_kb(channels, {c["id"] for c in missing}),
        )
        return None


# ══════════════════════════════════════════════════════════════════════
# TASKS
# ══════════════════════════════════════════════════════════════════════
"""Fon vazifalari: Premium eslatmalari, muddati tugashi, eskirgan to'lovlarni tozalash."""


INTERVAL = 600  # 10 daqiqa
_bg: set[asyncio.Task] = set()


async def send_teaser(bot: Bot, preview_for: int | None = None) -> int:
    """Rasmli VIP kino postini yuboradi. preview_for berilsa — faqat shu adminga (oddiy foydalanuvchi ko'rinishida)."""
    movie = await db_pick_vip_for_teaser()
    if not movie:
        return 0
    cost = movie["price"] or int(await db_get_setting("vip_cost") or 10)
    daily = int(await db_get_setting("premium_daily_coins") or 0)
    head = (f"💎 <b>Bugungi VIP kino</b>\n\n🎬 <b>{esc(movie['title'])}</b>\n"
            f"🔑 Kod: <code>{esc(movie['code'])}</code>\n💰 Narxi: <b>{cost} coin</b>\n\n")
    free_text = head + (f"⭐ Premium obunachilarga har kuni <b>{daily} coin</b> beriladi." if daily else
                        "⭐ Premium bilan ko'proq imkoniyat.")
    prem_text = head + "🪙 Premium coiningiz bilan hoziroq ko'ring!"
    ids = [preview_for] if preview_for else await db_broadcast_user_ids()
    sent = 0
    for uid in ids:
        premium = False if preview_for else bool(await db_get_premium(uid))
        for _ in range(2):
            try:
                await bot.send_photo(uid, movie["poster_id"], caption=prem_text if premium else free_text,
                                     reply_markup=kb_teaser_kb(movie["code"], premium, cost))
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await db_set_left(uid, True)
                break
            except TelegramAPIError:
                break
        await asyncio.sleep(0.05)
    if not preview_for:
        await db_mark_teased(movie["id"])
        await db_log(None, "vip_teaser", f"movie={movie['code']} sent={sent}")
    return sent


async def _send(bot: Bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text, reply_markup=kb_premium_btn())
    except TelegramAPIError:
        pass


async def tick(bot: Bot) -> None:
    await db_expire_stale_payments()
    hour = await db_get_setting("teaser_hour")
    if hour.isdigit() and datetime.now(TZ).hour == int(hour) \
            and await db_get_setting("teaser_last") != db_today_key():
        await db_set_setting("teaser_last", db_today_key())
        task = asyncio.create_task(send_teaser(bot))
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    daily = int(await db_get_setting("premium_daily_coins") or 0)
    if daily > 0:  # Premium obunachilarga kunlik coin
        today = db_today_key()
        for r in await db_premium_coin_candidates(today):
            await db_add_coins(r["user_id"], daily, "premium_daily")
            await db_mark_coin_date(r["user_id"], today)
    t = db_now()
    for r in await db_reminder_candidates():
        left = r["expires_at"] - t
        if left <= 86400 and not r["reminded1"]:
            await _send(bot, r["user_id"],
                        f"⏰ <b>Premium muddati tugashiga 1 kundan kam qoldi!</b>\n📅 {fmt_date(r['expires_at'])}\n\nUzaytirish uchun pastdagi tugmani bosing.")
            await db_mark_reminded(r["user_id"], 1)
        elif 86400 < left <= 3 * 86400 and not r["reminded3"]:
            await _send(bot, r["user_id"],
                        f"⏰ <b>Premium muddati tugashiga 3 kun qoldi.</b>\n📅 {fmt_date(r['expires_at'])}\n\nUzaytirish uchun pastdagi tugmani bosing.")
            await db_mark_reminded(r["user_id"], 3)
        await asyncio.sleep(0.05)
    for r in await db_expired_unnotified():
        await _send(bot, r["user_id"], "⌛ <b>Premium muddatingiz tugadi.</b>\nQayta faollashtirish uchun tarif tanlang.")
        await db_mark_expired_notified(r["user_id"])
        await asyncio.sleep(0.05)


async def background_loop(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("Fon vazifasida xato")
        await asyncio.sleep(INTERVAL)


async def process_deletes(bot: Bot) -> None:
    """VIP kinolarni belgilangan vaqtda o'chiradi va foydalanuvchiga xabar beradi."""
    for r in await db_due_deletes():
        deleted = False
        for mid in r["msg_ids"].split(","):
            try:
                await bot.delete_message(r["user_id"], int(mid))
                deleted = True
            except TelegramAPIError:
                pass
        await db_done_delete(r["id"])
        movie = await db_get_movie(r["movie_id"]) if r["movie_id"] else None
        if deleted and movie:
            cost = movie["price"] or int(await db_get_setting("vip_cost") or 10)
            try:
                await bot.send_message(
                    r["user_id"],
                    f"⏳ <b>VIP kino vaqti tugadi.</b>\n\n🎬 {esc(movie['title'])}\n\n"
                    "Xohlasangiz, yana sotib olishingiz mumkin.",
                    reply_markup=kb_vip_expired_kb(movie["code"], cost))
            except TelegramAPIError:
                pass


async def delete_loop(bot: Bot) -> None:
    while True:
        try:
            await process_deletes(bot)
        except Exception:
            log.exception("O'chirish vazifasida xato")
        await asyncio.sleep(20)


# ══════════════════════════════════════════════════════════════════════
# HANDLERS/COMMON
# ══════════════════════════════════════════════════════════════════════
"""Admin kirish (login), /admin va umumiy «Bekor qilish»."""


common_router = Router(name="common")

_fails: dict[int, list[float]] = {}
MAX_TRIES, LOCK_SEC = 5, 900


@common_router.message(F.text == kb_BTN_CANCEL)
async def cancel_any(m: Message, state: FSMContext):
    await state.clear()
    if await db_is_admin(m.from_user.id) and (m.text == kb_BTN_CANCEL):
        # admin oqimidan bekor qilsa — panelga, foydalanuvchi oqimida bo'lsa — menyuga qaytadi
        await m.answer("↩️ Bekor qilindi.", reply_markup=kb_admin_menu())
    else:
        await m.answer("↩️ Bekor qilindi.", reply_markup=kb_main_menu())


@common_router.message(F.text == f"/{config.admin_command}")
async def admin_login(m: Message, state: FSMContext):
    uid = m.from_user.id
    if await db_is_admin(uid):
        await state.clear()
        await m.answer("🛠 <b>Admin panel</b>", reply_markup=kb_admin_menu())
        return
    tries = [t for t in _fails.get(uid, []) if time.time() - t < LOCK_SEC]
    if len(tries) >= MAX_TRIES:
        await m.answer("⛔ Juda ko'p urinish. Keyinroq qayta urinib ko'ring.")
        return
    await state.set_state(AdminAuth.password)
    await m.answer("🔐 Parolni kiriting", reply_markup=kb_cancel_kb())


@common_router.message(AdminAuth.password, F.text)
async def admin_password(m: Message, state: FSMContext):
    uid = m.from_user.id
    txt = m.text.strip()
    if txt.startswith("/start"):
        await state.clear()
        await m.answer("↩️ Bekor qilindi.", reply_markup=kb_main_menu())
        return
    try:
        await m.delete()  # parol chatda qolmasin
    except Exception:
        pass
    if hmac.compare_digest(txt.encode(), config.admin_password.encode()):
        _fails.pop(uid, None)
        await db_add_admin(uid)
        await db_log(uid, "admin_login", "muvaffaqiyatli")
        await state.clear()
        await m.answer("✅ Xush kelibsiz, admin!\n\n🛠 <b>Admin panel</b>", reply_markup=kb_admin_menu())
        return
    tries = [t for t in _fails.get(uid, []) if time.time() - t < LOCK_SEC] + [time.time()]
    _fails[uid] = tries
    await db_log(uid, "admin_login_fail", f"urinish={len(tries)}")
    if len(tries) >= MAX_TRIES:
        await state.clear()
        await m.answer("⛔ Juda ko'p xato urinish. 15 daqiqadan keyin qayta urinib ko'ring.",
                       reply_markup=kb_main_menu())
    else:
        await m.answer(f"❌ Parol noto'g'ri. Qolgan urinishlar: {MAX_TRIES - len(tries)}")


@common_router.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    if await db_is_admin(m.from_user.id):
        await state.clear()
        await m.answer("🛠 <b>Admin panel</b>", reply_markup=kb_admin_menu())


# ══════════════════════════════════════════════════════════════════════
# HANDLERS/ADMIN
# ══════════════════════════════════════════════════════════════════════
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_tasks: set[asyncio.Task] = set()
ADMIN_BTNS = {
    kb_A_ADD_MOVIE, kb_A_DEL_MOVIE, kb_A_EDIT_MOVIE, kb_A_ADD_CHANNEL, kb_A_DEL_CHANNEL, kb_A_CARD,
    kb_A_PRICE, kb_A_PAYMENTS, kb_A_GRANT, kb_A_STORAGE, kb_A_PROMO, kb_A_BROADCAST, kb_A_BAN, kb_A_UNBAN, kb_A_STATS, kb_A_SETTINGS,
    kb_A_EXIT,
}
NOT_MENU = ~F.text.in_(ADMIN_BTNS)


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user) and await db_is_admin(user.id)


admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


async def _done(m: Message | CallbackQuery, state: FSMContext, text: str, bot: Bot | None = None):
    """Oqimni tugatib, admin menyusini qaytaradi."""
    await state.clear()
    chat_id = m.from_user.id
    target = bot or m.bot
    await target.send_message(chat_id, text, reply_markup=kb_admin_menu())


def _parse_video(m: Message):
    if m.video:
        return m.video.file_id, "video"
    if m.document:
        return m.document.file_id, "document"
    if m.text and m.text.strip().startswith(("http://", "https://")):
        return m.text.strip(), "link"
    return None


# ═════════════════════════ CHIQISH ═════════════════════════
@admin_router.message(F.text == kb_A_EXIT)
async def a_exit(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in config.admin_ids:
        await m.answer("🏠 Foydalanuvchi menyusi. Panelga qaytish: /admin", reply_markup=kb_main_menu())
        return
    await db_remove_admin(m.from_user.id)
    await db_log(m.from_user.id, "admin_logout")
    await m.answer("🚪 Admin paneldan chiqdingiz.", reply_markup=kb_main_menu())


# ═════════════════════════ KINO QO'SHISH ═════════════════════════
@admin_router.message(F.text == kb_A_ADD_MOVIE)
async def add_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddMovie.title)
    await m.answer("🎬 <b>Yangi kino</b>\n\n1️⃣ Kino nomini kiriting:", reply_markup=kb_cancel_kb())


@admin_router.message(AddMovie.title, F.text, NOT_MENU)
async def add_title(m: Message, state: FSMContext):
    title = m.text.strip()
    if not 1 <= len(title) <= 200:
        await m.answer("❗ Nom 1–200 belgidan iborat bo'lsin.")
        return
    await state.update_data(title=title)
    await state.set_state(AddMovie.code)
    await m.answer(f"2️⃣ Kino kodini kiriting.\nBo'sh kod: <code>{await db_next_code()}</code>")


@admin_router.message(AddMovie.code, F.text, NOT_MENU)
async def add_code(m: Message, state: FSMContext):
    code = m.text.strip()
    if not CODE_RE.fullmatch(code):
        await m.answer("❗ Kod faqat harf, raqam, «-» va «_» dan iborat bo'lsin (maks 32).")
        return
    if await db_get_movie_by_code(code):
        await m.answer("❌ Bu kod band. Boshqa kod kiriting.")
        return
    await state.update_data(code=code)
    await state.set_state(AddMovie.poster)
    await m.answer("3️⃣ Kino <b>rasmini</b> yuboring:")


@admin_router.message(AddMovie.poster, F.photo)
async def add_poster(m: Message, state: FSMContext):
    await state.update_data(poster=m.photo[-1].file_id)
    if (await state.get_data()).get("src_msg"):  # video yopiq kanaldan — video so'ralmaydi
        await state.set_state(AddMovie.vip)
        await m.answer("4️⃣ Kino turi:", reply_markup=kb_vip_kb())
        return
    await state.set_state(AddMovie.video)
    await m.answer("4️⃣ <b>Videoni</b> yuboring yoki video <b>havolasini</b> yozing:")


@admin_router.message(AddMovie.poster, NOT_MENU)
async def add_poster_bad(m: Message):
    await m.answer("❗ Iltimos, rasm yuboring.")


@admin_router.message(AddMovie.video, NOT_MENU)
async def add_video(m: Message, state: FSMContext):
    parsed = _parse_video(m)
    if not parsed:
        await m.answer("❗ Video, video-fayl yoki http(s) havola yuboring.")
        return
    await state.update_data(video=parsed[0], vtype=parsed[1],
                            dur=(m.video.duration if m.video else None))
    await state.set_state(AddMovie.vip)
    await m.answer("5️⃣ Kino turi:", reply_markup=kb_vip_kb())


async def _finish_add(bot: Bot, admin_id: int, state: FSMContext) -> None:
    d = await state.get_data()
    vip = bool(d.get("vip"))
    src_chat, src_msg = d.get("src_chat"), d.get("src_msg")
    problem = None
    if await db_get_movie_by_code(d["code"]):
        problem = "❌ Bu kod band bo'lib qoldi. Qaytadan urinib ko'ring."
    elif src_msg and await db_get_movie_by_src(src_chat, src_msg):
        problem = "❌ Bu video allaqachon kino sifatida qo'shilgan."
    if problem:
        await state.clear()
        await bot.send_message(admin_id, problem, reply_markup=kb_admin_menu())
        return
    mid = await db_add_movie(d["code"], d["title"], d["poster"], d.get("video"),
                             d.get("vtype", "channel" if src_msg else "video"), vip, src_chat, src_msg,
                             duration=d.get("dur"))
    await db_log(admin_id, "movie_add", f"id={mid} code={d['code']} title={d['title']} vip={vip}")
    await state.clear()
    mins = f"\n⏱ Davomiyligi: {round(d['dur'] / 60)} daqiqa" if d.get("dur") else ""
    await bot.send_message(
        admin_id, f"✅ <b>Kino saqlandi!</b>\n🎬 {esc(d['title'])}\n🔑 Kod: <code>{esc(d['code'])}</code>{mins}",
        reply_markup=kb_admin_menu())
    await bot.send_message(admin_id, "📢 Quyida kanalga yuboriladigan post ko'rinishi 👇")
    movie = await db_get_movie(mid)
    me = await bot.me()
    await bot.send_photo(admin_id, movie["poster_id"], caption=svc_channel_caption(movie, me.username),
                         reply_markup=kb_chpost_kb(mid))


@admin_router.callback_query(AddMovie.vip, F.data.startswith("vip:"))
async def add_vip(c: CallbackQuery, state: FSMContext, bot: Bot):
    vip = c.data.endswith("1")
    await state.update_data(vip=vip)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    d = await state.get_data()
    if vip and not d.get("dur"):  # VIP kino vaqti davomiylikka bog'liq
        await state.set_state(AddMovie.minutes)
        await bot.send_message(c.from_user.id,
                               "⏱ VIP kino <b>davomiyligini daqiqada</b> kiriting (masalan <code>95</code>):",
                               reply_markup=kb_cancel_kb())
        return
    await _finish_add(bot, c.from_user.id, state)


@admin_router.message(AddMovie.minutes, F.text, NOT_MENU)
async def add_minutes(m: Message, state: FSMContext, bot: Bot):
    t = m.text.strip()
    if not t.isdigit() or not 1 <= int(t) <= 1000:
        await m.answer("❗ 1 dan 1000 gacha daqiqa kiriting.")
        return
    await state.update_data(dur=int(t) * 60)
    await _finish_add(bot, m.from_user.id, state)


@admin_router.callback_query(F.data.startswith("chpost:"))
async def channel_post(c: CallbackQuery, bot: Bot):
    _, act, mid = c.data.split(":")
    base = c.message.html_text or ""
    if act == "no":
        await c.answer("Yuborilmadi")
        await c.message.edit_caption(caption=base + "\n\n🚫 <i>Kanalga yuborilmadi</i>", reply_markup=None)
        return
    movie = await db_get_movie(int(mid))
    target = channel_target(await db_get_setting("movie_channel"))
    if not movie or not target:
        await c.answer("❌ Kanal manzili aniqlanmadi. Sozlamalarda kino kanal havolasini ochiq (t.me/nom) qiling.",
                       show_alert=True)
        return
    me = await bot.me()
    url = f"https://t.me/{me.username}?start=m_{movie['code']}"
    try:
        await bot.send_photo(target, movie["poster_id"], caption=svc_channel_caption(movie, me.username),
                             reply_markup=kb_channel_post_kb(url))
    except TelegramAPIError as e:
        await c.answer(f"❌ Yuborib bo'lmadi: {e.message[:150]}\nBot kanalda admin bo'lishi kerak.", show_alert=True)
        return
    await db_log(c.from_user.id, "channel_post", f"movie={movie['code']}")
    await c.answer("✅ Kanalga yuborildi")
    await c.message.edit_caption(caption=base + "\n\n✅ <i>Kanalga yuborildi</i>", reply_markup=None)


# ═════════════════════════ KINO O'CHIRISH ═════════════════════════
@admin_router.message(F.text == kb_A_DEL_MOVIE)
async def del_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(DelMovie.code)
    await m.answer("🗑 O'chiriladigan <b>kino kodini</b> yozing:", reply_markup=kb_cancel_kb())


@admin_router.message(DelMovie.code, F.text, NOT_MENU)
async def del_code(m: Message, state: FSMContext):
    movie = await db_get_movie_by_code(m.text.strip())
    if not movie:
        await m.answer("❌ Bunday kod topilmadi. Qayta kiriting.")
        return
    await state.clear()
    await m.answer("Tasdiqlang:", reply_markup=kb_admin_menu())
    await m.answer(f"🗑 <b>{esc(movie['title'])}</b> (kod: <code>{esc(movie['code'])}</code>) o'chirilsinmi?",
                   reply_markup=kb_confirm_delete_kb(movie["id"]))


@admin_router.callback_query(F.data.startswith("del:"))
async def del_confirm(c: CallbackQuery):
    _, act, mid = c.data.split(":")
    if act == "no":
        await c.answer("Bekor qilindi")
        await c.message.edit_text("↩️ Bekor qilindi.")
        return
    movie = await db_get_movie(int(mid))
    if not movie:
        await c.answer("Allaqachon o'chirilgan", show_alert=True)
        return
    await db_delete_movie(movie["id"])
    await db_log(c.from_user.id, "movie_delete", f"code={movie['code']} title={movie['title']}")
    await c.answer("O'chirildi")
    await c.message.edit_text(f"✅ <b>{esc(movie['title'])}</b> o'chirildi.")


# ═════════════════════════ KINO TAHRIRLASH ═════════════════════════
@admin_router.message(F.text == kb_A_EDIT_MOVIE)
async def edit_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(EditMovie.code)
    await m.answer("✏ Tahrirlanadigan <b>kino kodini</b> yozing:", reply_markup=kb_cancel_kb())


@admin_router.message(EditMovie.code, F.text, NOT_MENU)
async def edit_code(m: Message, state: FSMContext):
    movie = await db_get_movie_by_code(m.text.strip())
    if not movie:
        await m.answer("❌ Bunday kod topilmadi. Qayta kiriting.")
        return
    await state.clear()
    await m.answer("Nimani o'zgartiramiz?", reply_markup=kb_admin_menu())
    await m.answer(
        f"🎬 <b>{esc(movie['title'])}</b>\n🔑 Kod: <code>{esc(movie['code'])}</code>\n"
        f"💎 Turi: {'VIP' if movie['is_vip'] else 'Oddiy'}\n👁 Ko'rishlar: {movie['views']}\n"
        f"⏱ Davomiyligi: {round(movie['duration'] / 60) if movie['duration'] else '—'} daqiqa\n"
        f"💰 Narxi: {movie['price'] or 'standart'} coin",
        reply_markup=kb_edit_kb(movie["id"], bool(movie["is_vip"])))


EDIT_PROMPTS = {
    "title": "📝 Yangi <b>nomni</b> kiriting:",
    "code": "🔑 Yangi <b>kodni</b> kiriting:",
    "poster": "🖼 Yangi <b>rasmni</b> yuboring:",
    "video": "🎞 Yangi <b>video</b> yoki <b>havola</b> yuboring:",
    "minutes": "⏱ Kino davomiyligini <b>daqiqada</b> kiriting:",
    "price": "💰 Kino narxini <b>coin</b>da kiriting (standart narx uchun «-»):",
}


@admin_router.callback_query(F.data.startswith("ed:"))
async def edit_pick(c: CallbackQuery, state: FSMContext, bot: Bot):
    _, field, mid = c.data.split(":")
    movie = await db_get_movie(int(mid))
    if not movie:
        await c.answer("Kino topilmadi", show_alert=True)
        return
    if field == "vip":
        new = 0 if movie["is_vip"] else 1
        await db_update_movie(movie["id"], is_vip=new)
        await db_log(c.from_user.id, "movie_edit", f"code={movie['code']} vip={new}")
        await c.answer("Yangilandi")
        await c.message.edit_reply_markup(reply_markup=kb_edit_kb(movie["id"], bool(new)))
        return
    await c.answer()
    await state.set_state(EditMovie.value)
    await state.update_data(mid=movie["id"], field=field)
    await bot.send_message(c.from_user.id, EDIT_PROMPTS[field], reply_markup=kb_cancel_kb())


@admin_router.message(EditMovie.value, NOT_MENU)
async def edit_value(m: Message, state: FSMContext):
    d = await state.get_data()
    movie = await db_get_movie(d["mid"])
    if not movie:
        await _done(m, state, "❌ Kino topilmadi.")
        return
    f = d["field"]
    if f == "title":
        if not m.text or not 1 <= len(m.text.strip()) <= 200:
            await m.answer("❗ Nom 1–200 belgi bo'lsin.")
            return
        await db_update_movie(movie["id"], title=m.text.strip())
    elif f == "code":
        code = (m.text or "").strip()
        if not CODE_RE.fullmatch(code):
            await m.answer("❗ Kod noto'g'ri formatda.")
            return
        other = await db_get_movie_by_code(code)
        if other and other["id"] != movie["id"]:
            await m.answer("❌ Bu kod band.")
            return
        await db_update_movie(movie["id"], code=code)
    elif f == "poster":
        if not m.photo:
            await m.answer("❗ Rasm yuboring.")
            return
        await db_update_movie(movie["id"], poster_id=m.photo[-1].file_id)
    elif f == "minutes":
        t = (m.text or "").strip()
        if not t.isdigit() or not 1 <= int(t) <= 1000:
            await m.answer("❗ 1 dan 1000 gacha daqiqa kiriting.")
            return
        await db_update_movie(movie["id"], duration=int(t) * 60)
    elif f == "price":
        t = (m.text or "").strip()
        if t == "-":
            await db_update_movie(movie["id"], price=None)
        elif t.isdigit() and 1 <= int(t) <= 100000:
            await db_update_movie(movie["id"], price=int(t))
        else:
            await m.answer("❗ 1–100000 oralig'ida raqam yoki «-» yuboring.")
            return
    else:
        parsed = _parse_video(m)
        if not parsed:
            await m.answer("❗ Video, fayl yoki havola yuboring.")
            return
        await db_update_movie(movie["id"], video_id=parsed[0], video_type=parsed[1],
                              src_chat=None, src_msg=None)
    await db_log(m.from_user.id, "movie_edit", f"code={movie['code']} field={f}")
    await _done(m, state, "✅ Kino yangilandi.")


# ═════════════════════════ MAJBURIY KANALLAR ═════════════════════════
@admin_router.message(F.text == kb_A_ADD_CHANNEL)
async def ch_add_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddChannel.ref)
    await m.answer(
        "📢 Kanalni yuboring:\n• <code>@username</code> yoki havola\n• kanal ID si\n• kanaldan <b>forward</b> qilingan xabar\n\n"
        "⚠️ Bot kanalda <b>admin</b> bo'lishi shart.", reply_markup=kb_cancel_kb())


@admin_router.message(AddChannel.ref, NOT_MENU)
async def ch_add(m: Message, state: FSMContext, bot: Bot):
    ref = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        ref = fo.chat.id
    elif m.text:
        ref = parse_chat_ref(m.text)
    if ref is None:
        await m.answer("❗ Kanal aniqlanmadi. @username, havola, ID yoki forward yuboring.")
        return
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError:
        await m.answer("❌ Kanal topilmadi yoki bot unda yo'q.")
        return
    if chat.type not in ("channel", "supergroup", "group"):
        await m.answer("❌ Bu kanal/guruh emas.")
        return
    me = await bot.me()
    try:
        mem = await bot.get_chat_member(chat.id, me.id)
        is_adm = mem.status in ("administrator", "creator")
    except TelegramAPIError:
        is_adm = False
    if not is_adm:
        await m.answer("❌ Avval botni shu kanalga <b>admin</b> qiling, so'ng qayta yuboring.")
        return
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            link = getattr(chat, "invite_link", None) or await bot.export_chat_invite_link(chat.id)
        except TelegramAPIError:
            link = None
    if not link:
        await m.answer("❌ Kanal havolasini olib bo'lmadi (botga «havola yaratish» huquqini bering).")
        return
    await db_add_channel(chat.id, chat.title or str(chat.id), link)
    await db_log(m.from_user.id, "channel_add", f"{chat.id} {chat.title}")
    await _done(m, state, f"✅ Majburiy kanal qo'shildi: <b>{esc(chat.title)}</b>")


async def _channels_text() -> str:
    chs = await db_list_channels()
    if not chs:
        return "📭 Majburiy kanallar yo'q."
    return "📢 <b>Majburiy kanallar:</b>\n\n" + "\n".join(
        f"{i}. {esc(c['title'])} — {esc(c['link'])}" for i, c in enumerate(chs, 1))


@admin_router.message(F.text == kb_A_DEL_CHANNEL)
async def ch_del_list(m: Message, state: FSMContext):
    await state.clear()
    chs = await db_list_channels()
    if not chs:
        await m.answer("📭 Majburiy kanallar yo'q.")
        return
    await m.answer("🗑 O'chiriladigan kanalni tanlang:", reply_markup=kb_channels_del_kb(chs))


@admin_router.callback_query(F.data.startswith("chdel:"))
async def ch_del(c: CallbackQuery):
    ch = await db_get_channel(int(c.data.split(":")[1]))
    if ch:
        await db_delete_channel(ch["id"])
        await db_log(c.from_user.id, "channel_delete", f"{ch['chat_id']} {ch['title']}")
    await c.answer("O'chirildi")
    chs = await db_list_channels()
    if chs:
        await c.message.edit_reply_markup(reply_markup=kb_channels_del_kb(chs))
    else:
        await c.message.edit_text("📭 Majburiy kanallar yo'q.")


# ═════════════════════════ KARTA ═════════════════════════
async def start_card(bot: Bot, chat_id: int, state: FSMContext):
    await state.clear()
    await state.set_state(CardStates.number)
    cur = await db_get_setting("card_number")
    await bot.send_message(chat_id, f"💳 Joriy karta: <code>{esc(cur)}</code>\n\nYangi <b>karta raqamini</b> kiriting (16 xona):",
                           reply_markup=kb_cancel_kb())


@admin_router.message(F.text == kb_A_CARD)
async def card_start(m: Message, state: FSMContext, bot: Bot):
    await start_card(bot, m.chat.id, state)


@admin_router.message(CardStates.number, F.text, NOT_MENU)
async def card_number(m: Message, state: FSMContext):
    d = re.sub(r"\D", "", m.text)
    if len(d) != 16:
        await m.answer("❗ Karta raqami 16 ta raqamdan iborat bo'lishi kerak.")
        return
    await state.update_data(number=fmt_card(d))
    await state.set_state(CardStates.owner)
    await m.answer("👤 <b>Karta egasining ismini</b> kiriting:")


@admin_router.message(CardStates.owner, F.text, NOT_MENU)
async def card_owner(m: Message, state: FSMContext):
    if not 2 <= len(m.text.strip()) <= 80:
        await m.answer("❗ Ism 2–80 belgi bo'lsin.")
        return
    await state.update_data(owner=m.text.strip())
    await state.set_state(CardStates.bank)
    await m.answer("🏦 <b>Bank nomini</b> kiriting (kerak bo'lmasa «-»):")


@admin_router.message(CardStates.bank, F.text, NOT_MENU)
async def card_bank(m: Message, state: FSMContext):
    d = await state.get_data()
    bank = "" if m.text.strip() == "-" else m.text.strip()[:60]
    await db_set_setting("card_number", d["number"])
    await db_set_setting("card_owner", d["owner"])
    await db_set_setting("card_bank", bank)
    await db_log(m.from_user.id, "card_change", d["number"][-4:])
    await _done(m, state, "✅ Karta yangilandi:\n\n" + svc_card_block(d["number"], d["owner"], bank))


# ═════════════════════════ TARIFLAR ═════════════════════════
async def show_tariffs(bot: Bot, chat_id: int, state: FSMContext):
    await state.clear()
    ts = await db_list_tariffs()
    await bot.send_message(
        chat_id,
        "🏷 <b>Tariflar</b>\nO'zgartirish uchun tarifni tanlang yoki yangisini qo'shing.\n\n"
        "<i>VIP limit — Premium foydalanuvchi oyiga nechta VIP kino ko'ra olishi (0 = cheksiz).</i>",
        reply_markup=kb_tariffs_admin_kb(ts))


@admin_router.message(F.text == kb_A_PRICE)
async def tariffs_menu(m: Message, state: FSMContext, bot: Bot):
    await show_tariffs(bot, m.chat.id, state)


@admin_router.callback_query(F.data == "tf:list")
async def tf_list(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    try:
        await c.message.delete()
    except TelegramAPIError:
        pass
    await show_tariffs(bot, c.from_user.id, state)


@admin_router.callback_query(F.data.startswith("tf:edit:"))
async def tf_edit(c: CallbackQuery):
    t = await db_get_tariff(int(c.data.split(":")[2]))
    if not t:
        await c.answer("Tarif topilmadi", show_alert=True)
        return
    await c.answer()
    await c.message.edit_text(
        f"🏷 <b>{esc(t['title'])}</b>\n📅 Muddat: {t['days']} kun\n💰 Narx: {money(t['price'])} so'm\n"
        f"👑 VIP oylik limit: {t['vip_limit'] or 'cheksiz'}", reply_markup=kb_tariff_edit_kb(t["id"]))


TF_PROMPTS = {
    "price": "💰 Yangi <b>narxni</b> kiriting (so'm):",
    "days": "📅 Yangi <b>muddatni</b> kiriting (kun):",
    "title": "📝 Yangi <b>nomni</b> kiriting:",
    "vip_limit": "👑 Oyiga nechta VIP kino ko'rish mumkin? (0 = cheksiz)",
}


@admin_router.callback_query(F.data.startswith("tf:f:"))
async def tf_field(c: CallbackQuery, state: FSMContext, bot: Bot):
    _, _, field, tid = c.data.split(":")
    await c.answer()
    await state.set_state(TariffStates.value)
    await state.update_data(field=field, tid=int(tid))
    await bot.send_message(c.from_user.id, TF_PROMPTS[field], reply_markup=kb_cancel_kb())


@admin_router.message(TariffStates.value, F.text, NOT_MENU)
async def tf_value(m: Message, state: FSMContext):
    d = await state.get_data()
    field, t = d["field"], m.text.strip()
    if field == "title":
        if not 1 <= len(t) <= 40:
            await m.answer("❗ Nom 1–40 belgi bo'lsin.")
            return
        val = t
    else:
        t = re.sub(r"[\s,.']", "", t)
        lim = {"days": (1, 3650), "price": (1000, 1_000_000_000), "vip_limit": (0, 10000)}[field]
        if not t.isdigit() or not lim[0] <= int(t) <= lim[1]:
            await m.answer(f"❗ {lim[0]} dan {lim[1]} gacha raqam kiriting.")
            return
        val = int(t)
    await db_update_tariff(d["tid"], **{field: val})
    await db_log(m.from_user.id, "tariff_edit", f"id={d['tid']} {field}={val}")
    await _done(m, state, "✅ Tarif yangilandi.")


@admin_router.callback_query(F.data.startswith("tf:del:"))
async def tf_del(c: CallbackQuery):
    tid = int(c.data.split(":")[2])
    await db_update_tariff(tid, active=0)
    await db_log(c.from_user.id, "tariff_delete", str(tid))
    await c.answer("O'chirildi")
    await c.message.edit_text("🗑 Tarif o'chirildi (eski to'lovlar saqlanadi).")


@admin_router.callback_query(F.data == "tf:new")
async def tf_new(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    await state.clear()
    await state.set_state(TariffStates.title)
    await bot.send_message(c.from_user.id, "🏷 <b>Yangi tarif</b>\n\n1️⃣ Tarif nomini kiriting (masalan: <code>3 OY</code>):",
                           reply_markup=kb_cancel_kb())


@admin_router.message(TariffStates.title, F.text, NOT_MENU)
async def tf_title(m: Message, state: FSMContext):
    if not 1 <= len(m.text.strip()) <= 40:
        await m.answer("❗ Nom 1–40 belgi bo'lsin.")
        return
    await state.update_data(title=m.text.strip())
    await state.set_state(TariffStates.days)
    await m.answer("2️⃣ Necha <b>kunga</b>? (masalan: <code>90</code>)")


@admin_router.message(TariffStates.days, F.text, NOT_MENU)
async def tf_days(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not 1 <= int(t) <= 3650:
        await m.answer("❗ 1 dan 3650 gacha kun kiriting.")
        return
    await state.update_data(days=int(t))
    await state.set_state(TariffStates.price)
    await m.answer("3️⃣ <b>Narxi</b> (so'm):")


@admin_router.message(TariffStates.price, F.text, NOT_MENU)
async def tf_price(m: Message, state: FSMContext):
    t = re.sub(r"[\s,.']", "", m.text)
    if not t.isdigit() or not 1000 <= int(t) <= 1_000_000_000:
        await m.answer("❗ Narx 1 000 dan 1 000 000 000 gacha bo'lsin.")
        return
    await state.update_data(price=int(t))
    await state.set_state(TariffStates.vip_limit)
    await m.answer("4️⃣ Oyiga nechta <b>VIP kino</b> ko'rish mumkin? (0 = cheksiz)")


@admin_router.message(TariffStates.vip_limit, F.text, NOT_MENU)
async def tf_vip_limit(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not 0 <= int(t) <= 10000:
        await m.answer("❗ 0 dan 10000 gacha raqam kiriting.")
        return
    d = await state.get_data()
    tid = await db_add_tariff(d["title"], d["days"], d["price"], int(t))
    await db_log(m.from_user.id, "tariff_add", f"id={tid} {d['title']} {d['days']}k {d['price']}")
    await _done(m, state, f"✅ Tarif qo'shildi: <b>{esc(d['title'])}</b> — {d['days']} kun, {money(d['price'])} so'm")


# ═════════════════════════ PROMOKODLAR ═════════════════════════
async def show_promos(bot: Bot, chat_id: int, state: FSMContext):
    await state.clear()
    ps = await db_list_promos()
    await bot.send_message(
        chat_id, "🎟 <b>Promokodlar</b>\n<i>Tugmani bossangiz promokod o'chadi. Ko'rinishi: kod • mukofot • ishlatilgan/limit</i>",
        reply_markup=kb_promos_kb(ps))


@admin_router.message(F.text == kb_A_PROMO)
async def promo_menu(m: Message, state: FSMContext, bot: Bot):
    await show_promos(bot, m.chat.id, state)


@admin_router.callback_query(F.data.startswith("pc:del:"))
async def promo_del(c: CallbackQuery):
    code = c.data.split(":", 2)[2]
    await db_delete_promo(code)
    await db_log(c.from_user.id, "promo_delete", code)
    await c.answer("O'chirildi")
    await c.message.edit_reply_markup(reply_markup=kb_promos_kb(await db_list_promos()))


@admin_router.callback_query(F.data == "pc:new")
async def promo_new(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    await state.clear()
    await state.set_state(PromoAdmin.code)
    await bot.send_message(c.from_user.id,
                           "🎟 <b>Yangi promokod</b>\n\n1️⃣ Kodni yozing (3–20 ta harf/raqam) yoki avtomatik uchun <code>auto</code>:",
                           reply_markup=kb_cancel_kb())


@admin_router.message(PromoAdmin.code, F.text, NOT_MENU)
async def promo_code(m: Message, state: FSMContext):
    code = m.text.strip()
    if code.lower() == "auto":
        code = secrets.token_hex(4).upper()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", code):
        await m.answer("❗ Kod 3–20 ta harf, raqam, «-» yoki «_» bo'lsin.")
        return
    if await db_get_promo(code):
        await m.answer("❌ Bu kod band. Boshqasini kiriting.")
        return
    await state.update_data(code=code)
    await m.answer("2️⃣ Mukofot turi:", reply_markup=kb_promo_kind_kb())


@admin_router.callback_query(F.data.startswith("pc:kind:"))
async def promo_kind(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if "code" not in d:
        await c.answer("Avval kodni kiriting.", show_alert=True)
        return
    kind = c.data.split(":")[2]
    await c.answer()
    await state.update_data(kind=kind)
    await state.set_state(PromoAdmin.amount)
    await bot.send_message(c.from_user.id, "3️⃣ Necha coin?" if kind == "coin" else "3️⃣ Necha kun Premium?")


@admin_router.message(PromoAdmin.amount, F.text, NOT_MENU)
async def promo_amount(m: Message, state: FSMContext):
    d = await state.get_data()
    t = m.text.strip()
    hi = 100000 if d["kind"] == "coin" else 3650
    if not t.isdigit() or not 1 <= int(t) <= hi:
        await m.answer(f"❗ 1 dan {hi} gacha raqam kiriting.")
        return
    await state.update_data(amount=int(t))
    await state.set_state(PromoAdmin.max_uses)
    await m.answer("4️⃣ Necha kishi ishlata oladi? (0 = cheksiz)")


@admin_router.message(PromoAdmin.max_uses, F.text, NOT_MENU)
async def promo_max(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not 0 <= int(t) <= 1_000_000:
        await m.answer("❗ 0 dan 1 000 000 gacha raqam kiriting.")
        return
    d = await state.get_data()
    if not await db_create_promo(d["code"], d["kind"], d["amount"], int(t)):
        await _done(m, state, "❌ Bu kod band bo'lib qoldi.")
        return
    await db_log(m.from_user.id, "promo_add", f"{d['code']} {d['kind']} {d['amount']} max={t}")
    what = f"🪙 {d['amount']} coin" if d["kind"] == "coin" else f"⭐ {d['amount']} kun Premium"
    await _done(m, state, f"✅ Promokod yaratildi:\n\n<code>{esc(d['code'])}</code>\n{what}\nLimit: {int(t) or 'cheksiz'}")


# ═════════════════════════ PREMIUM TO'LOVLARI ═════════════════════════
@admin_router.message(F.text == kb_A_PAYMENTS)
async def payments_list(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    rows = await db_list_pending()
    if not rows:
        await m.answer("📭 Kutayotgan to'lovlar yo'q.")
        return
    await m.answer(f"💰 Kutayotgan to'lovlar: <b>{len(rows)}</b>")
    for p in rows:
        await svc_send_payment_to(bot, m.chat.id, p)


async def _fulfill(bot: Bot, p, admin_id: int) -> None:
    """Tasdiqlangan to'lov uchun xaridni beradi (Premium / coin / reklama)."""
    uid, plan = p["user_id"], p["plan"]
    if plan == "coin":
        amount = int(await db_get_setting("coin_pack_amount") or 100) * (p["qty"] or 1)
        await db_add_coins(uid, amount, f"sotib:{p['id']}")
        text = f"✅ <b>To'lov tasdiqlandi.</b>\n🪙 Hisobingizga <b>+{amount} coin</b> qo'shildi."
    elif plan == "ad":
        text = "✅ <b>To'lov tasdiqlandi.</b>\n📣 Reklamangiz foydalanuvchilarga yuborilmoqda."
        ad_id = await db_add_ad(admin_id, "paid", p["src_chat"], p["src_msg"])
        task = asyncio.create_task(_broadcast(
            bot, ad_id, admin_id, {"src_chat": p["src_chat"], "src_msg": p["src_msg"], "fwd": False},
            notify_user=uid))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    else:
        t = await db_get_tariff_by_key(plan)
        exp = await db_grant_premium(uid, plan, t["days"] if t else 30)
        text = (f"✅ <b>Premium muvaffaqiyatli yoqildi.</b>\n"
                f"📅 Amal qilish muddati: <b>{fmt_date(exp)}</b> gacha")
    try:
        await bot.send_message(uid, text, reply_markup=kb_main_menu())
    except TelegramAPIError:
        pass


@admin_router.callback_query(F.data.startswith("pa:"))
async def payment_decide(c: CallbackQuery, bot: Bot):
    _, act, pid = c.data.split(":")
    p = await db_get_payment(int(pid))
    base = c.message.html_text or ""

    async def mark(text: str):
        try:
            await c.message.edit_caption(caption=base + f"\n\n{text}", reply_markup=None)
        except TelegramBadRequest:
            pass

    if not p or p["status"] != "pending":
        await c.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        await mark("ℹ️ <i>Allaqachon ko'rib chiqilgan</i>")
        return

    if act == "no":
        if await db_reject_payment(p["id"], c.from_user.id):
            await db_log(c.from_user.id, "payment_reject", f"id={p['id']} user={p['user_id']}")
            await c.answer("Bekor qilindi")
            await mark(f"❌ <i>Bekor qilindi</i> (admin {c.from_user.id})")
            try:
                await bot.send_message(p["user_id"], "❌ <b>To'lov tasdiqlanmadi.</b>")
            except TelegramAPIError:
                pass
        return

    if not p["check_file_id"]:
        await c.answer("❌ Chek yo'q — tasdiqlab bo'lmaydi.", show_alert=True)
        return

    mismatch = p["amount_paid"] != p["amount_due"]
    if mismatch and act == "ok":
        # Foydalanuvchi summani adashib yozgan bo'lishi mumkin — chekni ko'rib admin qaror qiladi
        await c.answer(
            f"⚠️ Kiritilgan summa mos emas!\nKerak: {money(p['amount_due'])}\n"
            f"Kiritilgan: {money(p['amount_paid'] or 0)}\n\nAgar chekda summa to'g'ri bo'lsa, "
            "pastdagi tugma bilan tasdiqlang.", show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=kb_pay_force_kb(p["id"]))
        except TelegramBadRequest:
            pass
        return

    if not await db_approve_payment(p["id"], c.from_user.id):
        await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await _fulfill(bot, p, c.from_user.id)
    await db_stat_inc("payments")
    await db_stat_inc("revenue", p["amount_due"])
    await db_log(c.from_user.id, "payment_force_approve" if mismatch else "payment_approve",
                 f"id={p['id']} user={p['user_id']} plan={p['plan']} kerak={p['amount_due']} yozilgan={p['amount_paid']}")
    await c.answer("✅ Tasdiqlandi")
    await mark(f"✅ <i>Tasdiqlandi</i> (admin {c.from_user.id})" + (" — chek bo'yicha" if mismatch else ""))


# ═════════════════════════ YOPIQ VIDEO KANAL ═════════════════════════
@admin_router.message(F.text == kb_A_STORAGE)
async def storage_start(m: Message, state: FSMContext):
    await state.clear()
    cur = await db_get_setting("storage_chat")
    text = "📦 <b>Video saqlash kanali</b>\n\n"
    text += f"Joriy kanal ID: <code>{esc(cur)}</code>\n\n" if cur else "Hozircha o'rnatilmagan.\n\n"
    text += ("1) Yopiq (private) kanal oching.\n"
    "2) Botni unga <b>admin</b> qiling.\n"
    "3) Kanal sozlamalarida «Kontentni saqlashni cheklash» <b>o'chiq</b> bo'lsin.\n"
    "4) Kanaldan biror xabarni bu yerga <b>forward</b> qiling yoki kanal ID sini (<code>-100...</code>) yuboring.\n\n"
    "O'chirish uchun «-» yuboring.")
    await state.set_state(StorageStates.ref)
    await m.answer(text, reply_markup=kb_cancel_kb())


@admin_router.message(StorageStates.ref, NOT_MENU)
async def storage_set(m: Message, state: FSMContext, bot: Bot):
    if m.text and m.text.strip() == "-":
        await db_set_setting("storage_chat", "")
        await db_log(m.from_user.id, "storage_off")
        await _done(m, state, "🗑 Video saqlash kanali o'chirildi.")
        return
    ref = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        ref = fo.chat.id
    elif m.text:
        ref = parse_chat_ref(m.text)
    if ref is None:
        await m.answer("❗ Kanal aniqlanmadi. Kanaldan xabar forward qiling yoki ID yuboring.")
        return
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError:
        await m.answer("❌ Kanal topilmadi yoki bot unda yo'q.")
        return
    if chat.type != "channel":
        await m.answer("❌ Bu kanal emas.")
        return
    me = await bot.me()
    try:
        mem = await bot.get_chat_member(chat.id, me.id)
        ok = mem.status in ("administrator", "creator")
    except TelegramAPIError:
        ok = False
    if not ok:
        await m.answer("❌ Avval botni shu kanalga <b>admin</b> qiling, so'ng qayta yuboring.")
        return
    await db_set_setting("storage_chat", str(chat.id))
    await db_log(m.from_user.id, "storage_set", str(chat.id))
    await _done(m, state, f"✅ Video saqlash kanali o'rnatildi: <b>{esc(chat.title)}</b>\n\n"
                          "Endi shu kanalga video tashlasangiz, bot sizga yuboradi va kino qo'shishni so'raydi.")


@admin_router.channel_post(F.video | F.document)
async def storage_post(m: Message, bot: Bot):
    storage = await db_get_setting("storage_chat")
    if not storage or str(m.chat.id) != storage:
        return
    if m.document and not (m.document.mime_type or "").startswith("video/"):
        return
    if await db_get_movie_by_src(m.chat.id, m.message_id):
        return
    if m.video:
        await db_save_src_video(m.chat.id, m.message_id, m.video.duration)
    media = m.video or m.document
    info = f"📦 {(media.file_size or 0) / 1024 / 1024:.1f} MB"
    if m.video and m.video.duration:
        s = m.video.duration
        info += f" • ⏱ {s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    for aid in await db_admin_ids():
        try:
            await bot.copy_message(
                aid, m.chat.id, m.message_id,
                caption=f"📥 <b>Kanaldan yangi video</b>\n{info}\n\nKino sifatida qo'shamizmi?",
                reply_markup=kb_storage_kb(m.message_id))
        except TelegramAPIError as e:
            log.warning("Videoni adminga yuborib bo'lmadi (%s): %s", aid, e)


@admin_router.callback_query(F.data.startswith("sv:"))
async def storage_pick(c: CallbackQuery, state: FSMContext, bot: Bot):
    _, act, mid = c.data.split(":")
    if act == "no":
        await c.answer("O'tkazib yuborildi")
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    storage = int(await db_get_setting("storage_chat") or 0)
    if not storage:
        await c.answer("Video kanal o'rnatilmagan.", show_alert=True)
        return
    old = await db_get_movie_by_src(storage, int(mid))
    if old:
        await c.answer(f"Bu video allaqachon qo'shilgan (kod: {old['code']}).", show_alert=True)
        return
    await c.answer()
    await state.clear()
    await state.set_state(AddMovie.title)
    sv = await db_get_src_video(storage, int(mid))
    await state.update_data(src_chat=storage, src_msg=int(mid), dur=(sv["duration"] if sv else None))
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await bot.send_message(c.from_user.id, "🎬 <b>Kanaldagi video — yangi kino</b>\n\n1️⃣ Kino nomini kiriting:",
                           reply_markup=kb_cancel_kb())


# ═════════════════════════ PREMIUM / COIN BERISH (qo'lda) ═════════════════════════
async def _give_premium(bot: Bot, admin_id: int, uid: int, plan: str, days: int) -> int:
    exp = await db_grant_premium(uid, plan, days)
    await db_log(admin_id, "premium_grant_admin", f"user={uid} days={days}")
    try:
        await bot.send_message(
            uid, f"✅ <b>Premium muvaffaqiyatli yoqildi.</b>\n📅 Amal qilish muddati: <b>{fmt_date(exp)}</b> gacha",
            reply_markup=kb_main_menu())
    except TelegramAPIError:
        pass
    return exp


@admin_router.message(F.text == kb_A_GRANT)
async def grant_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GrantStates.user_id)
    await m.answer("🎁 Foydalanuvchining <b>Telegram ID</b> sini yozing:", reply_markup=kb_cancel_kb())


@admin_router.message(GrantStates.user_id, F.text, NOT_MENU)
async def grant_id(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit():
        await m.answer("❗ Faqat raqamli Telegram ID kiriting.")
        return
    uid = int(t)
    u = await db_get_user(uid)
    p = await db_get_premium(uid)
    name = esc(u["first_name"]) if u else "— (botga hali kirmagan)"
    status = f"✅ faol, {fmt_date(p['expires_at'])} gacha" if p else "❌ yo'q"
    coins = await db_get_coins(uid) if u else 0
    await state.clear()
    await m.answer(f"👤 {name}\n🆔 <code>{uid}</code>\n⭐ Premium: {status}\n🪙 Coin: {coins}",
                   reply_markup=kb_admin_menu())
    await m.answer("Nima beramiz?", reply_markup=kb_grant_kb(uid, await db_list_tariffs()))


@admin_router.callback_query(F.data.startswith("gr:"))
async def grant_pick(c: CallbackQuery, state: FSMContext, bot: Bot):
    _, act, uid_s = c.data.split(":")
    uid = int(uid_s)
    if act == "rev":
        ok = await db_revoke_premium(uid)
        await db_log(c.from_user.id, "premium_revoke", f"user={uid} ok={ok}")
        await c.answer("Olib tashlandi" if ok else "Bu foydalanuvchida Premium yo'q", show_alert=not ok)
        if ok:
            await c.message.edit_text(f"🗑 <code>{uid}</code> dan Premium olib tashlandi.")
        return
    if act in ("days", "coin"):
        await c.answer()
        await state.set_state(GrantStates.days if act == "days" else GrantStates.coins)
        await state.update_data(uid=uid)
        await bot.send_message(c.from_user.id,
                               "✏ Necha <b>kunga</b> Premium beramiz? (1–3650)" if act == "days"
                               else "🪙 Necha <b>coin</b> beramiz? (1–100000)", reply_markup=kb_cancel_kb())
        return
    t = await db_get_tariff_by_key(act)
    if not t:
        await c.answer()
        return
    exp = await _give_premium(bot, c.from_user.id, uid, t["key"], t["days"])
    await c.answer("✅ Berildi")
    await c.message.edit_text(f"✅ <code>{uid}</code> ga <b>{esc(t['title'])}</b> Premium berildi.\n📅 {fmt_date(exp)} gacha")


@admin_router.message(GrantStates.days, F.text, NOT_MENU)
async def grant_days(m: Message, state: FSMContext, bot: Bot):
    t = m.text.strip()
    if not t.isdigit() or not (1 <= int(t) <= 3650):
        await m.answer("❗ 1 dan 3650 gacha kun kiriting.")
        return
    uid = (await state.get_data())["uid"]
    exp = await _give_premium(bot, m.from_user.id, uid, "admin", int(t))
    await _done(m, state, f"✅ <code>{uid}</code> ga <b>{t} kunlik</b> Premium berildi.\n📅 {fmt_date(exp)} gacha")


@admin_router.message(GrantStates.coins, F.text, NOT_MENU)
async def grant_coins(m: Message, state: FSMContext, bot: Bot):
    t = m.text.strip()
    if not t.isdigit() or not (1 <= int(t) <= 100000):
        await m.answer("❗ 1 dan 100000 gacha coin kiriting.")
        return
    uid = (await state.get_data())["uid"]
    if not await db_add_coins(uid, int(t), f"admin:{m.from_user.id}"):
        await _done(m, state, "❌ Bu foydalanuvchi botga hali kirmagan.")
        return
    await db_log(m.from_user.id, "coin_grant_admin", f"user={uid} coins={t}")
    try:
        await bot.send_message(uid, f"🎁 Sizga <b>+{t} coin</b> berildi!")
    except TelegramAPIError:
        pass
    await _done(m, state, f"✅ <code>{uid}</code> ga <b>{t} coin</b> berildi.")


# ═════════════════════════ BLOKLASH ═════════════════════════
@admin_router.message(F.text == kb_A_BAN)
async def ban_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BlockStates.user_id)
    await m.answer("🚫 Bloklanadigan <b>Telegram ID</b> ni yozing:", reply_markup=kb_cancel_kb())


@admin_router.message(BlockStates.user_id, F.text, NOT_MENU)
async def ban_do(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit():
        await m.answer("❗ Faqat raqamli Telegram ID kiriting.")
        return
    uid = int(t)
    if await db_is_admin(uid):
        await m.answer("❌ Adminni bloklab bo'lmaydi.")
        return
    await db_ban_user(uid, m.from_user.id)
    await db_log(m.from_user.id, "user_ban", str(uid))
    await _done(m, state, f"🚫 <code>{uid}</code> bloklandi.")


@admin_router.message(F.text == kb_A_UNBAN)
async def unban_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(UnblockStates.user_id)
    await m.answer("✅ Blokdan chiqariladigan <b>Telegram ID</b> ni yozing:", reply_markup=kb_cancel_kb())


@admin_router.message(UnblockStates.user_id, F.text, NOT_MENU)
async def unban_do(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit():
        await m.answer("❗ Faqat raqamli Telegram ID kiriting.")
        return
    ok = await db_unban_user(int(t))
    await db_log(m.from_user.id, "user_unban", f"{t} ok={ok}")
    await _done(m, state, f"✅ <code>{t}</code> blokdan chiqarildi." if ok else "ℹ️ Bu ID bloklanganlar ro'yxatida yo'q.")


# ═════════════════════════ REKLAMA YUBORISH ═════════════════════════
@admin_router.message(F.text == kb_A_BROADCAST)
async def bc_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BroadcastStates.content)
    await m.answer("📨 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring:\n"
                   "matn, rasm, video yoki <b>forward</b> qilingan xabar.", reply_markup=kb_cancel_kb())


@admin_router.message(BroadcastStates.content, NOT_MENU)
async def bc_content(m: Message, state: FSMContext, bot: Bot):
    fwd = getattr(m, "forward_origin", None) is not None
    kind = "forward" if fwd else m.content_type.value if hasattr(m.content_type, "value") else str(m.content_type)
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id, fwd=fwd, kind=kind)
    n = len(await db_broadcast_user_ids())
    await m.answer("👀 <b>Ko'rinishi:</b>", reply_markup=kb_admin_menu())
    try:
        if fwd:
            await bot.forward_message(m.chat.id, m.chat.id, m.message_id)
        else:
            await bot.copy_message(m.chat.id, m.chat.id, m.message_id)
    except TelegramAPIError as e:
        await m.answer(f"❌ Bu xabarni yuborib bo'lmaydi: {e.message[:150]}")
        return
    await m.answer(f"Yuboramizmi? Qabul qiluvchilar: <b>{n}</b>", reply_markup=kb_broadcast_kb())


@admin_router.callback_query(F.data.startswith("bc:"))
async def bc_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if c.data == "bc:no" or "src_msg" not in d:
        await state.clear()
        await c.answer("Bekor qilindi")
        await c.message.edit_text("↩️ Reklama bekor qilindi.")
        return
    await state.clear()
    ad_id = await db_add_ad(c.from_user.id, d["kind"], d["src_chat"], d["src_msg"])
    await db_log(c.from_user.id, "broadcast_start", f"ad={ad_id} kind={d['kind']}")
    await c.answer("Boshlandi")
    await c.message.edit_text("🚀 Yuborish boshlandi. Tugagach hisobot beraman.")
    task = asyncio.create_task(_broadcast(bot, ad_id, c.from_user.id, d))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _broadcast(bot: Bot, ad_id: int, admin_id: int, d: dict, notify_user: int | None = None) -> None:
    ids = await db_broadcast_user_ids()
    sent = failed = 0

    async def send(uid: int) -> bool:
        for _ in range(2):
            try:
                if d["fwd"]:
                    await bot.forward_message(uid, d["src_chat"], d["src_msg"])
                else:
                    await bot.copy_message(uid, d["src_chat"], d["src_msg"])
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await db_set_left(uid, True)
                return False
            except TelegramAPIError:
                return False
        return False

    for uid in ids:
        if await send(uid):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 xabar/soniya
    await db_finish_ad(ad_id, sent, failed)
    await db_log(admin_id, "broadcast_done", f"ad={ad_id} sent={sent} failed={failed}")
    try:
        await bot.send_message(admin_id, f"📨 <b>Reklama yuborildi</b>\n\n✅ Yetkazildi: {sent}\n❌ Xato/bloklagan: {failed}",
                               reply_markup=kb_admin_menu())
    except TelegramAPIError:
        pass
    if notify_user:
        try:
            await bot.send_message(notify_user, f"📣 Reklamangiz <b>{sent}</b> kishiga yetkazildi.")
        except TelegramAPIError:
            pass


# ═════════════════════════ STATISTIKA ═════════════════════════
@admin_router.message(F.text == kb_A_STATS)
async def stats(m: Message, state: FSMContext):
    await state.clear()
    s = await db_stats_summary()
    await m.answer(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{s['total']}</b>\n"
        f"🆕 Bugun qo'shilganlar: <b>{s['new_today']}</b>\n"
        f"⭐ Premium foydalanuvchilar: <b>{s['premium']}</b>\n"
        f"👤 Oddiy foydalanuvchilar: <b>{s['regular']}</b>\n"
        f"🚫 Bloklanganlar: <b>{s['banned']}</b>\n"
        f"🚪 Botni tark etganlar: <b>{s['left']}</b>\n"
        f"💳 Bugungi to'lovlar: <b>{s['pay_today']}</b> ta ({money(s['rev_today'])} so'm)\n"
        f"💰 Umumiy daromad: <b>{money(s['revenue'])}</b> so'm\n"
        f"👁 Bugungi ko'rilgan kinolar: <b>{s['views_today']}</b>\n"
        f"🎬 Jami kinolar soni: <b>{s['movies']}</b>")


# ═════════════════════════ SOZLAMALAR ═════════════════════════
EDITABLE = {
    "start_text": "👋 Start matni",
    "bot_name": "🤖 Bot nomi",
    "premium_text": "⭐ Premium matni",
    "ad_text": "📣 Reklama matni",
    "daily_limit": "🔢 Kunlik limit",
    "movie_channel": "📢 Kino kanal havolasi",
    "teaser_hour": "🕕 Kunlik VIP post vaqti",
    "vip_cost": "💎 VIP kino narxi (coin)",
    "coin_pack_amount": "🪙 Coin paketi (necha coin)",
    "coin_pack_price": "💵 Coin paketi narxi (so'm)",
    "premium_daily_coins": "🎁 Premium kunlik coin",
    "ref_bonus": "👥 Referal bonusi (coin)",
    "ad_price": "📣 Reklama narxi (so'm)",
    "vip_expire_mult": "⏳ VIP vaqt koeffitsienti",
}
INT_RANGES = {
    "vip_cost": (1, 100000), "coin_pack_amount": (1, 100000), "coin_pack_price": (1000, 1_000_000_000),
    "premium_daily_coins": (0, 10000), "ref_bonus": (0, 10000), "ad_price": (1000, 1_000_000_000),
    "vip_expire_mult": (1, 100),
}
HTML_KEYS = {"start_text", "premium_text", "ad_text"}


@admin_router.message(F.text == kb_A_SETTINGS)
async def settings_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙ <b>Sozlamalar</b>\nNimani o'zgartiramiz?", reply_markup=kb_settings_kb(EDITABLE))


@admin_router.callback_query(F.data.startswith("st:"))
async def settings_pick(c: CallbackQuery, state: FSMContext, bot: Bot):
    key = c.data[3:]
    await c.answer()
    if key == "card":
        await start_card(bot, c.from_user.id, state)
    elif key == "prices":
        await show_tariffs(bot, c.from_user.id, state)
    elif key == "teaser_test":
        n = await send_teaser(bot, preview_for=c.from_user.id)
        if not n:
            await bot.send_message(c.from_user.id, "❗ Rasmli VIP kino yo'q. Avval VIP kino qo'shing (oddiy emas, «💎 VIP» tanlang).")
    elif key == "channels":
        await bot.send_message(c.from_user.id,
                               await _channels_text() + "\n\nQo'shish/o'chirish uchun admin menyudagi tugmalardan foydalaning.")
    elif key in EDITABLE:
        cur = (await db_get_setting(key))[:900]
        extra = "\n<i>Formatlash (qalin, kursiv, havola) saqlanadi.</i>" if key in HTML_KEYS else ""
        if key in ("start_text",):
            extra += "\nMaxsus so'zlar: <code>{name}</code> — ism, <code>{bot}</code> — bot nomi."
        if key == "ad_text":
            extra += "\nTozalash uchun «-» yuboring."
        if key == "vip_expire_mult":
            extra += "\nVIP kino ko'rish vaqti = kino davomiyligi × shu son (foydalanuvchiga aytilmaydi)."
        if key == "teaser_hour":
            extra += "\nSoatni 0–23 oralig'ida yozing (Toshkent vaqti), masalan <code>18</code>. O'chirish uchun «-» yuboring."
        await state.set_state(SettingStates.value)
        await state.update_data(key=key)
        await bot.send_message(c.from_user.id,
                               f"{EDITABLE[key]}\n\nJoriy qiymat:\n<code>{esc(cur) or '—'}</code>\n\nYangi qiymatni yuboring.{extra}",
                               reply_markup=kb_cancel_kb())


@admin_router.message(SettingStates.value, F.text, NOT_MENU)
async def settings_value(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    val = m.html_text.strip() if key in HTML_KEYS else m.text.strip()
    if key == "ad_text" and val == "-":
        val = ""
    if key == "daily_limit":
        if not val.isdigit() or not (1 <= int(val) <= 10000):
            await m.answer("❗ Limit 1 dan 10000 gacha raqam bo'lsin.")
            return
    elif key == "movie_channel":
        if not re.match(r"^https?://t\.me/\S+$", val):
            await m.answer("❗ Havola <code>https://t.me/...</code> ko'rinishida bo'lsin.")
            return
    elif key == "bot_name":
        if not 1 <= len(val) <= 60:
            await m.answer("❗ Nom 1–60 belgi bo'lsin.")
            return
    elif key in INT_RANGES:
        lo, hi = INT_RANGES[key]
        val = re.sub(r"[\s,.']", "", val)
        if not val.isdigit() or not lo <= int(val) <= hi:
            await m.answer(f"❗ {lo} dan {hi} gacha raqam kiriting.")
            return
    elif key == "teaser_hour":
        if val == "-":
            val = "off"
        elif not val.isdigit() or not (0 <= int(val) <= 23):
            await m.answer("❗ Soat 0 dan 23 gacha raqam bo'lsin (yoki o'chirish uchun «-»).")
            return
    elif len(val) > 3500:
        await m.answer("❗ Matn juda uzun (maks 3500 belgi).")
        return
    if key in HTML_KEYS and val:
        try:
            preview = val.replace("{name}", "Ism").replace("{bot}", "Bot")
            await m.answer("👀 <b>Ko'rinishi:</b>\n\n" + preview)
        except TelegramBadRequest:
            await m.answer("❌ Matn formati xato. Qayta yuboring.")
            return
    await db_set_setting(key, val)
    await db_log(m.from_user.id, "setting_change", key)
    await _done(m, state, f"✅ {EDITABLE[key]} yangilandi.")


# ══════════════════════════════════════════════════════════════════════
# HANDLERS/USER
# ══════════════════════════════════════════════════════════════════════
user_router = Router(name="user")
user_router.message.middleware(SubscriptionGate())
user_router.callback_query.middleware(SubscriptionGate())

_promo_fails: dict[int, list[float]] = {}


# ───────────────────────── yordamchi funksiyalar ─────────────────────────
async def send_welcome(bot: Bot, user) -> None:
    name = await db_get_setting("bot_name")
    tpl = await db_get_setting("start_text")
    text = tpl.replace("{name}", esc(user.first_name)).replace("{bot}", esc(name))
    try:
        await bot.send_message(user.id, text, reply_markup=kb_main_menu())
    except TelegramBadRequest:
        await bot.send_message(user.id, text, parse_mode=None, reply_markup=kb_main_menu())


def _link_url(movie) -> str | None:
    if movie["video_type"] == "link" and (movie["video_id"] or "").startswith(("http://", "https://")):
        return movie["video_id"]
    return None


async def reward_referral(bot: Bot, user_id: int) -> None:
    """Yangi foydalanuvchi majburiy obunadan o'tgach, taklif qilganga bonus beradi."""
    ref = await db_get_pending_referrer(user_id)
    if not ref:
        return
    await db_mark_ref_rewarded(user_id)
    bonus = int(await db_get_setting("ref_bonus") or 0)
    if bonus > 0 and await db_add_coins(ref, bonus, f"referal:{user_id}"):
        try:
            await bot.send_message(
                ref, f"🎉 Havolangiz orqali yangi foydalanuvchi qo'shildi!\n🪙 <b>+{bonus} coin</b>")
        except TelegramAPIError:
            pass


async def _vip_cost(movie) -> int:
    return movie["price"] or int(await db_get_setting("vip_cost") or 10)


async def _send_files(bot: Bot, user_id: int, movie, caption: str, mk, protect: bool) -> list[int]:
    """Kinoni yuboradi, yuborilgan xabar id larini qaytaradi."""
    vt = movie["video_type"]
    if vt == "link":
        msg = await bot.send_message(user_id, caption + f"\n\n🔗 {esc(movie['video_id'])}", reply_markup=mk)
        return [msg.message_id]
    if vt == "channel":
        r = await bot.copy_message(user_id, movie["src_chat"], movie["src_msg"], caption=caption,
                                   reply_markup=mk, protect_content=protect)
        return [r.message_id]
    if vt == "document":
        msg = await bot.send_document(user_id, movie["video_id"], caption=caption, reply_markup=mk,
                                      protect_content=protect)
        return [msg.message_id]
    msg = await bot.send_video(user_id, movie["video_id"], caption=caption, reply_markup=mk,
                               supports_streaming=True, protect_content=protect)
    return [msg.message_id]


async def deliver_movie(bot: Bot, user_id: int, code: str, confirmed: bool = False) -> bool:
    movie = await db_get_movie_by_code(code)
    if not movie:
        await bot.send_message(user_id, "❌ Bunday kod topilmadi.")
        return False
    is_adm = await db_is_admin(user_id)
    prem = None if is_adm else await db_get_premium(user_id)
    if movie["is_vip"] and not is_adm:
        return await _deliver_vip(bot, user_id, movie, prem, confirmed)

    premium = bool(is_adm or prem)
    limit = int(await db_get_setting("daily_limit") or 10)
    used, reset = await db_get_usage(user_id)
    if not premium and used >= limit:
        left = max(reset - db_now(), 0)
        await bot.send_message(
            user_id,
            "❌ <b>Bugungi limit tugadi.</b>\n\n"
            "⭐ Premium sotib olishingiz mumkin.\n"
            f"🔄 Limit {left // 3600} soat {left % 3600 // 60} daqiqadan keyin yangilanadi.",
            reply_markup=kb_premium_btn())
        return False

    me = await bot.me()
    is_fav = await db_is_favorite(user_id, movie["id"])
    vip = " 💎" if movie["is_vip"] else ""
    quota = "" if premium else f"\n📊 Limit: {used + 1}/{limit}"
    caption = (f"🎬 <b>{esc(movie['title'])}</b>{vip}\n🔑 Kod: <code>{esc(movie['code'])}</code>"
               f"{quota}\n\n🤖 @{me.username}")
    try:
        mk = kb_movie_kb(movie["id"], is_fav, _link_url(movie),
                         upsell=(not premium and used + 1 >= limit - 2))
        await _send_files(bot, user_id, movie, caption, mk, protect=False)
    except TelegramAPIError as e:
        log.error("Kino yuborilmadi (%s): %s", code, e)
        await bot.send_message(user_id, "⚠️ Kino vaqtincha yuborilmadi. Keyinroq urinib ko'ring.")
        return False
    await db_register_view(user_id, movie["id"])
    return True


async def _deliver_vip(bot: Bot, user_id: int, movie, prem, confirmed: bool) -> bool:
    cost = await _vip_cost(movie)
    coins = await db_get_coins(user_id)

    if prem:  # tarifdagi oylik VIP limit
        t = await db_get_tariff_by_key(prem["plan"])
        vl = t["vip_limit"] if t else 0
        if vl and await db_vip_used_this_month(user_id) >= vl:
            await bot.send_message(
                user_id, f"📅 <b>Bu oy VIP kino limiti tugadi</b> ({vl} ta).\nKeyingi oyda yangilanadi.")
            return False

    if coins < cost:
        await bot.send_message(
            user_id,
            f"💎 <b>VIP kino</b>\n\n🎬 {esc(movie['title'])}\n💰 Narxi: <b>{cost} coin</b>\n"
            f"🪙 Sizda: <b>{coins} coin</b>\n\nCoin yetarli emas.",
            reply_markup=kb_vip_nocoin_kb())
        return False

    if not confirmed:
        await bot.send_message(
            user_id,
            f"💎 <b>VIP kino</b>\n\n🎬 {esc(movie['title'])}\n💰 Narxi: <b>{cost} coin</b>\n"
            f"🪙 Sizda: <b>{coins} coin</b>\n\nDavom etamizmi?",
            reply_markup=kb_vip_confirm_kb(movie["code"], cost))
        return False

    if not await db_spend_coins(user_id, cost, f"vip:{movie['code']}"):
        await bot.send_message(user_id, "❌ Coin yetarli emas.", reply_markup=kb_vip_nocoin_kb())
        return False

    me = await bot.me()
    is_fav = await db_is_favorite(user_id, movie["id"])
    caption = (f"🎬 <b>{esc(movie['title'])}</b> 💎\n🔑 Kod: <code>{esc(movie['code'])}</code>\n"
               f"🪙 Qoldi: {coins - cost} coin\n\n🤖 @{me.username}")
    try:
        ids = await _send_files(bot, user_id, movie, caption,
                                kb_movie_kb(movie["id"], is_fav, _link_url(movie)), protect=True)
    except TelegramAPIError as e:
        log.error("VIP kino yuborilmadi (%s): %s", movie["code"], e)
        await db_add_coins(user_id, cost, f"qaytarildi:{movie['code']}")
        await bot.send_message(user_id, "⚠️ Kino vaqtincha yuborilmadi. Coin qaytarildi.")
        return False

    await db_register_view(user_id, movie["id"])
    await db_vip_inc(user_id)
    # Ko'rish vaqti: kino davomiyligi × koeffitsient (foydalanuvchiga aytilmaydi)
    dur = movie["duration"] or int(await db_get_setting("vip_default_minutes") or 120) * 60
    mult = int(await db_get_setting("vip_expire_mult") or 6)
    delay = min(max(dur * mult, 15 * 60), 47 * 3600)  # Telegram 48 soatdan keyin o'chirishga ruxsat bermaydi
    await db_schedule_delete(user_id, ids, db_now() + delay, movie["id"])
    return True


async def _upsell(user_id: int) -> bool:
    if await db_is_admin(user_id) or await db_get_premium(user_id):
        return False
    limit = int(await db_get_setting("daily_limit") or 10)
    used, _ = await db_get_usage(user_id)
    return used >= limit - 2


async def show_premium(bot: Bot, user_id: int) -> None:
    tariffs = await db_list_tariffs()
    p = await db_get_premium(user_id)
    head = (f"✅ <b>Sizda Premium faol</b> — {fmt_date(p['expires_at'])} gacha.\n"
            "Muddatni uzaytirishingiz mumkin.\n\n") if p else ""
    daily = int(await db_get_setting("premium_daily_coins") or 0)
    coin_line = f"\n🪙 Har kuni <b>{daily} coin</b> (haftasiga {daily * 7})" if daily else ""
    limits = "".join(f"\n👑 {esc(t['title'])}: oyiga {t['vip_limit']} ta VIP kino"
                     for t in tariffs if t["vip_limit"])
    if not tariffs:
        await bot.send_message(user_id, "⭐ Hozircha tariflar mavjud emas.")
        return
    text = (f"⭐ <b>PREMIUM</b>\n\n{head}{await db_get_setting('premium_text')}\n\n"
            "✅ Majburiy obunasiz ishlaydi\n✅ Cheksiz kino\n✅ Kunlik limit yo'q"
            f"{coin_line}{limits}\n\n💎 <b>Tarifni tanlang:</b>")
    try:
        await bot.send_message(user_id, text, reply_markup=kb_premium_kb(tariffs))
    except TelegramBadRequest:
        await bot.send_message(user_id, text, parse_mode=None, reply_markup=kb_premium_kb(tariffs))


async def show_coins(bot: Bot, user_id: int) -> None:
    coins = await db_get_coins(user_id)
    amount = int(await db_get_setting("coin_pack_amount") or 100)
    price = int(await db_get_setting("coin_pack_price") or 10000)
    cost = int(await db_get_setting("vip_cost") or 10)
    daily = int(await db_get_setting("premium_daily_coins") or 0)
    prem = f"\n⭐ Premium obunachilarga har kuni <b>{daily} coin</b> beriladi (haftasiga {daily * 7})." if daily else ""
    await bot.send_message(
        user_id,
        f"🪙 <b>Coin</b>\n\n💰 Balansingiz: <b>{coins} coin</b>\n\n"
        f"💎 VIP kino narxi: odatda <b>{cost} coin</b>{prem}\n\n"
        f"🛒 <b>Coin sotib olish:</b>\n{amount} coin = {money(price)} so'm",
        reply_markup=kb_coin_kb(amount, price))


async def payment_screen(bot: Bot, user_id: int, pay, title: str) -> None:
    card = await db_get_setting("card_number")
    owner = await db_get_setting("card_owner")
    bank = await db_get_setting("card_bank")
    minutes = await db_get_setting("reserve_minutes")
    await db_log(user_id, "payment_reserve", f"id={pay['id']} plan={pay['plan']} sum={pay['amount_due']}")
    await bot.send_message(
        user_id,
        f"💳 <b>To'lov</b>\n\n📦 {title}\n"
        f"💰 Sizning summangiz: <code>{pay['amount_due']}</code> so'm\n\n"
        f"{svc_card_block(card, owner, bank)}\n"
        "⚠️ <b>Sizga berilgan aynan shu summani yuboring.</b>\n"
        "Boshqa summa yuborilsa, to'lov tasdiqlanmasligi mumkin.\n\n"
        "⏱ Admin 2 daqiqa ichida tekshiradi.\n"
        f"<i>Summa {minutes} daqiqa davomida faqat sizga biriktirilgan.</i>",
        reply_markup=kb_pay_kb(pay["id"]))


# ───────────────────────── /start ─────────────────────────
@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, bot: Bot, command: CommandObject):
    await state.clear()
    await send_welcome(bot, m.from_user)
    await reward_referral(bot, m.from_user.id)
    arg = command.args or ""
    if arg.startswith("m_"):
        await deliver_movie(bot, m.from_user.id, arg[2:])


@user_router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, state: FSMContext, bot: Bot):
    missing = await svc_not_joined(bot, c.from_user.id, force=True)
    if missing:
        await c.answer("❌ Hali barcha kanallarga a'zo bo'lmadingiz!", show_alert=True)
        channels = await db_list_channels()
        try:
            await c.message.edit_reply_markup(
                reply_markup=kb_subscribe_kb(channels, {x["id"] for x in missing}))
        except TelegramBadRequest:
            pass
        return
    await c.answer("✅ Rahmat!")
    try:
        await c.message.delete()
    except TelegramAPIError:
        pass
    data = await state.get_data()
    code = data.pop("pending_code", None)
    await state.set_data(data)
    await send_welcome(bot, c.from_user)
    await reward_referral(bot, c.from_user.id)
    if code:
        await deliver_movie(bot, c.from_user.id, code)


# ───────────────────────── asosiy menyu (har qanday holatda ishlaydi) ─────────────────────────
@user_router.message(F.text == kb_BTN_MOVIE)
async def btn_movie(m: Message, state: FSMContext):
    await state.set_state(UserStates.code)
    await m.answer("🎬 <b>Kino kodini kiriting.</b>\n\nMasalan: <code>2514</code>",
                   reply_markup=kb_main_menu())


@user_router.message(F.text == kb_BTN_SEARCH)
async def btn_search(m: Message, state: FSMContext):
    await state.set_state(UserStates.search)
    await m.answer("🔎 <b>Kino nomini yozing.</b>\n\nMasalan: <i>Forsaj</i>", reply_markup=kb_main_menu())


@user_router.message(F.text == kb_BTN_PREMIUM)
async def btn_premium(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await show_premium(bot, m.from_user.id)


@user_router.message(F.text == kb_BTN_COIN)
async def btn_coin(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await show_coins(bot, m.from_user.id)


@user_router.message(F.text == kb_BTN_REF)
async def btn_ref(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=r_{m.from_user.id}"
    bonus = int(await db_get_setting("ref_bonus") or 0)
    cnt = await db_count_referrals(m.from_user.id)
    reward = f"Har bir yangi do'stingiz uchun <b>{bonus} coin</b> olasiz.\n\n" if bonus else ""
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎬 Eng yaxshi kinolar boti!')}"
    await m.answer(
        f"👥 <b>Do'st taklif qilish</b>\n\n{reward}🔗 Sizning havolangiz:\n<code>{link}</code>\n\n"
        f"👤 Taklif qilinganlar: <b>{cnt}</b>\n🪙 Ishlangan coin: <b>{cnt * bonus}</b>\n\n"
        "<i>Bonus do'stingiz botga kirib, majburiy obunadan o'tgach beriladi.</i>",
        reply_markup=kb_share_kb(share))


@user_router.message(F.text == kb_BTN_PROMO)
async def btn_promo(m: Message, state: FSMContext):
    await state.set_state(PromoStates.code)
    await m.answer("🎟 <b>Promokodni kiriting:</b>", reply_markup=kb_cancel_kb())


@user_router.message(F.text == kb_BTN_CHANNEL)
async def btn_channel(m: Message, state: FSMContext):
    await state.clear()
    link = await db_get_setting("movie_channel")
    mk = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Kanalga o'tish", url=link)]])
    await m.answer("📢 <b>Kino kanalimiz</b>\n\nYangi kinolar shu yerda e'lon qilinadi.", reply_markup=mk)


@user_router.message(F.text == kb_BTN_ADS)
async def btn_ads(m: Message, state: FSMContext):
    await state.clear()
    ad = (await db_get_setting("ad_text")).strip()
    await m.answer(ad if ad else "📣 Hozircha reklama mavjud emas.", reply_markup=kb_ad_page_kb())


@user_router.message(F.text == kb_BTN_PROFILE)
async def btn_profile(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    p = await db_get_premium(u.id)
    used, _ = await db_get_usage(u.id)
    limit = await db_get_setting("daily_limit")
    status = "✅ Faol" if p else "❌ Yo'q"
    until = f"\n📅 Premium tugash sanasi: <b>{fmt_date(p['expires_at'])}</b>" if p else ""
    vip_line = ""
    if p:
        t = await db_get_tariff_by_key(p["plan"])
        if t and t["vip_limit"]:
            vip_line = f"\n👑 Bu oy VIP kino: <b>{await db_vip_used_this_month(u.id)}/{t['vip_limit']}</b>"
    await m.answer(
        "👤 <b>Profil</b>\n\n"
        f"📛 Ism: {esc(u.first_name)}\n"
        f"🆔 Telegram ID: <code>{u.id}</code>\n"
        f"🔗 Username: {'@' + esc(u.username) if u.username else '—'}\n"
        f"⭐ Premium: {status}{until}\n"
        f"🪙 Coin: <b>{await db_get_coins(u.id)}</b>\n"
        f"🎬 Bugungi ko'rilgan kinolar: <b>{used}/{'∞' if p else limit}</b>{vip_line}")


@user_router.message(F.text == kb_BTN_FAVS)
async def btn_favs(m: Message, state: FSMContext):
    await state.clear()
    rows = await db_list_favorites(m.from_user.id)
    if not rows:
        await m.answer("❤️ Sevimli kinolar yo'q.\nKino ostidagi «❤️ Sevimlilarga qo'shish» tugmasini bosing.")
        return
    await m.answer("❤️ <b>Sevimli kinolaringiz:</b>", reply_markup=kb_movies_list_kb(rows))


@user_router.message(F.text == kb_BTN_HISTORY)
async def btn_history(m: Message, state: FSMContext):
    await state.clear()
    rows = await db_list_history(m.from_user.id)
    if not rows:
        await m.answer("📜 Siz hali kino ko'rmagansiz.")
        return
    await m.answer("📜 <b>Oxirgi ko'rgan kinolaringiz:</b>", reply_markup=kb_movies_list_kb(rows))


# ───────────────────────── callbacklar ─────────────────────────
@user_router.callback_query(F.data == "premium")
async def cb_premium(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_premium(bot, c.from_user.id)


@user_router.callback_query(F.data == "coin")
async def cb_coin(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_coins(bot, c.from_user.id)


@user_router.callback_query(F.data.startswith("mv:"))
async def cb_movie(c: CallbackQuery, bot: Bot):
    await c.answer()
    await deliver_movie(bot, c.from_user.id, c.data[3:])


@user_router.callback_query(F.data.startswith("vb:"))
async def cb_vip_buy(c: CallbackQuery, bot: Bot):
    await c.answer()
    try:
        await c.message.delete()
    except TelegramAPIError:
        pass
    await deliver_movie(bot, c.from_user.id, c.data[3:], confirmed=True)


@user_router.callback_query(F.data == "vbno")
async def cb_vip_no(c: CallbackQuery):
    await c.answer("Bekor qilindi")
    try:
        await c.message.delete()
    except TelegramAPIError:
        pass


@user_router.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery):
    mid = int(c.data[4:])
    movie = await db_get_movie(mid)
    if not movie:
        await c.answer("Kino topilmadi", show_alert=True)
        return
    now_fav = await db_toggle_favorite(c.from_user.id, mid)
    await c.answer("❤️ Sevimlilarga qo'shildi" if now_fav else "💔 Sevimlilardan olib tashlandi")
    try:
        await c.message.edit_reply_markup(
            reply_markup=kb_movie_kb(mid, now_fav, _link_url(movie), await _upsell(c.from_user.id)))
    except TelegramBadRequest:
        pass


async def _busy(c: CallbackQuery) -> bool:
    if await db_get_pending_for_user(c.from_user.id):
        await c.answer("⏳ Sizning to'lov so'rovingiz admin tomonidan tekshirilmoqda.", show_alert=True)
        return True
    return False


@user_router.callback_query(F.data.startswith("plan:"))
async def cb_plan(c: CallbackQuery, bot: Bot):
    t = await db_get_tariff_by_key(c.data[5:])
    if not t or not t["active"]:
        await c.answer("Bu tarif mavjud emas.", show_alert=True)
        return
    if await _busy(c):
        return
    pay = await db_reserve_payment(c.from_user.id, t["key"], t["price"])
    if not pay:
        await c.answer("⚠️ Hozir band. Birozdan keyin urinib ko'ring.", show_alert=True)
        return
    await c.answer()
    await payment_screen(bot, c.from_user.id, pay, f"⭐ Premium — <b>{esc(t['title'])}</b>")


@user_router.callback_query(F.data.startswith("coinbuy:"))
async def cb_coinbuy(c: CallbackQuery, bot: Bot):
    q = c.data.split(":")[1]
    if q not in ("1", "3", "5", "10"):
        await c.answer()
        return
    if await _busy(c):
        return
    amount = int(await db_get_setting("coin_pack_amount") or 100)
    price = int(await db_get_setting("coin_pack_price") or 10000)
    pay = await db_reserve_payment(c.from_user.id, "coin", price * int(q), qty=int(q))
    if not pay:
        await c.answer("⚠️ Hozir band. Birozdan keyin urinib ko'ring.", show_alert=True)
        return
    await c.answer()
    await payment_screen(bot, c.from_user.id, pay, f"🪙 Coin — <b>{amount * int(q)} ta</b>")


@user_router.callback_query(F.data == "adbuy")
async def cb_adbuy(c: CallbackQuery, state: FSMContext, bot: Bot):
    if await _busy(c):
        return
    price = int(await db_get_setting("ad_price") or 50000)
    await c.answer()
    await state.set_state(AdOrder.content)
    await bot.send_message(
        c.from_user.id,
        f"📣 <b>Reklama berish</b>\n\n💰 Narxi: <b>{money(price)} so'm</b>\n"
        "Reklamangiz tasdiqlangach barcha foydalanuvchilarga yuboriladi.\n\n"
        "Reklama xabarini yuboring (matn, rasm yoki video):", reply_markup=kb_cancel_kb())


@user_router.message(AdOrder.content)
async def ad_content(m: Message, state: FSMContext, bot: Bot):
    if not (m.text or m.photo or m.video):
        await m.answer("❗ Matn, rasm yoki video yuboring.")
        return
    if await db_get_pending_for_user(m.from_user.id):
        await state.clear()
        await m.answer("⏳ Sizning to'lov so'rovingiz admin tomonidan tekshirilmoqda.", reply_markup=kb_main_menu())
        return
    price = int(await db_get_setting("ad_price") or 50000)
    pay = await db_reserve_payment(m.from_user.id, "ad", price, src_chat=m.chat.id, src_msg=m.message_id)
    await state.clear()
    if not pay:
        await m.answer("⚠️ Hozir band. Birozdan keyin urinib ko'ring.", reply_markup=kb_main_menu())
        return
    await m.answer("✅ Reklamangiz qabul qilindi. Endi to'lovni amalga oshiring 👇", reply_markup=kb_main_menu())
    await payment_screen(bot, m.from_user.id, pay, "📣 Reklama joylash")


@user_router.callback_query(F.data.startswith("paycancel:"))
async def cb_paycancel(c: CallbackQuery, state: FSMContext):
    await db_cancel_payment(int(c.data.split(":")[1]), c.from_user.id)
    await state.clear()
    await c.answer("Bekor qilindi")
    try:
        await c.message.edit_text("❌ To'lov so'rovi bekor qilindi.")
    except TelegramBadRequest:
        pass


@user_router.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery, state: FSMContext, bot: Bot):
    pid = int(c.data.split(":")[1])
    p = await db_get_payment(pid)
    if not p or p["user_id"] != c.from_user.id or p["status"] != "created":
        await c.answer("Bu so'rov eskirgan. Qaytadan boshlang.", show_alert=True)
        return
    await db_touch_payment(pid)
    await c.answer()
    await state.set_state(PayStates.tg_id)
    await state.update_data(pid=pid)
    await bot.send_message(c.from_user.id, "🆔 <b>Telegram ID</b> raqamingizni yuboring.",
                           reply_markup=kb_tgid_kb())


# ───────────────────────── to'lov bosqichlari ─────────────────────────
@user_router.message(PayStates.tg_id)
async def pay_tg_id(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if txt == kb_BTN_MY_ID:
        val = str(m.from_user.id)
    elif txt.isdigit() and 5 <= len(txt) <= 15:
        val = txt
    else:
        await m.answer("❗ Telegram ID faqat raqamlardan iborat. Qayta yuboring yoki tugmani bosing.")
        return
    await state.update_data(tg_id=val)
    await state.set_state(PayStates.phone)
    await m.answer("📱 <b>Telefon raqamingizni</b> yuboring.", reply_markup=kb_phone_kb())


@user_router.message(PayStates.phone)
async def pay_phone(m: Message, state: FSMContext):
    raw = m.contact.phone_number if m.contact else (m.text or "")
    digits = re.sub(r"[\s\-()]", "", raw)
    if not re.fullmatch(r"\+?\d{9,15}", digits):
        await m.answer("❗ Telefon raqami noto'g'ri. Masalan: <code>+998901234567</code>")
        return
    if not digits.startswith("+"):
        digits = "+" + digits
    await state.update_data(phone=digits)
    await state.set_state(PayStates.amount)
    data = await state.get_data()
    p = await db_get_payment(data["pid"])
    await m.answer(f"💰 <b>Necha so'm tashladingiz?</b>\nSizga berilgan summa: <code>{p['amount_due']}</code>",
                   reply_markup=kb_cancel_kb())


@user_router.message(PayStates.amount, F.text)
async def pay_amount(m: Message, state: FSMContext):
    t = re.sub(r"[\s,.']", "", m.text)
    if not t.isdigit() or not (100 <= int(t) <= 1_000_000_000):
        await m.answer("❗ Summani faqat raqam bilan yozing. Masalan: <code>8003</code>")
        return
    await state.update_data(paid=int(t))
    await state.set_state(PayStates.check)
    await m.answer("📷 <b>To'lov chekini yuboring</b> (rasm yoki PDF).")


@user_router.message(PayStates.check)
async def pay_check(m: Message, state: FSMContext, bot: Bot):
    if m.photo:
        fid, uid, ctype = m.photo[-1].file_id, m.photo[-1].file_unique_id, "photo"
    elif m.document and (m.document.mime_type or "").startswith(("image/", "application/pdf")):
        fid, uid, ctype = m.document.file_id, m.document.file_unique_id, "document"
    else:
        await m.answer("❗ Chek yuborilmasdan to'lov tasdiqlanmaydi. Iltimos, chekni rasm ko'rinishida yuboring.")
        return
    d = await state.get_data()
    p = await db_get_payment(d["pid"])
    if not p or p["status"] != "created":
        await state.clear()
        await m.answer("⚠️ So'rov muddati tugagan. Qaytadan boshlang.", reply_markup=kb_main_menu())
        return

    dup = await db_find_payment_by_unique(uid)  # takroriy (soxta) chekni aniqlash
    if dup:
        await db_log(m.from_user.id, "duplicate_receipt", f"yangi={p['id']} eski={dup['id']}")
        await svc_notify_admins(
            bot, f"⚠️ <b>Takroriy chek urinishi</b>\n\n👤 <code>{m.from_user.id}</code> avval yuborilgan "
                 f"chekni qayta yubordi.\nEski to'lov: #{dup['id']} (foydalanuvchi <code>{dup['user_id']}</code>, "
                 f"holati: {dup['status']})")
        await m.answer("⚠️ <b>Bu chek avval yuborilgan.</b>\nIltimos, o'z to'lovingizning yangi chekini yuboring.")
        return

    ok = await db_submit_payment(p["id"], d["tg_id"], d["phone"], d["paid"], fid, ctype, uid)
    await state.clear()
    if not ok:
        await m.answer("⚠️ Xatolik yuz berdi. Qaytadan urinib ko'ring.", reply_markup=kb_main_menu())
        return
    await db_set_phone(m.from_user.id, d["phone"])
    await db_log(m.from_user.id, "payment_submit",
                 f"id={p['id']} kerak={p['amount_due']} tolangan={d['paid']}")
    await m.answer("✅ <b>So'rovingiz adminga yuborildi.</b>\n⏳ Admin 2 daqiqa ichida tekshiradi.",
                   reply_markup=kb_main_menu())
    await svc_notify_admins_payment(bot, p["id"])


# ───────────────────────── promokod ─────────────────────────
@user_router.message(PromoStates.code, F.text)
async def promo_redeem(m: Message, state: FSMContext):
    uid = m.from_user.id
    fails = [t for t in _promo_fails.get(uid, []) if time.time() - t < 3600]
    if len(fails) >= 10:
        await m.answer("⛔ Juda ko'p xato urinish. Keyinroq qayta urinib ko'ring.")
        return
    res, p = await db_redeem_promo(m.text.strip(), uid)
    if res == "none":
        _promo_fails[uid] = fails + [time.time()]
        await m.answer("❌ Bunday promokod yo'q. Qayta kiriting.")
        return
    await state.clear()
    if res == "used":
        await m.answer("⚠️ Siz bu promokoddan allaqachon foydalangansiz.", reply_markup=kb_main_menu())
    elif res == "exhausted":
        await m.answer("⚠️ Bu promokodning limiti tugagan.", reply_markup=kb_main_menu())
    elif p["kind"] == "coin":
        await db_add_coins(uid, p["amount"], f"promo:{p['code']}")
        await db_log(uid, "promo_redeem", f"{p['code']} coin={p['amount']}")
        await m.answer(f"🎉 Promokod qabul qilindi!\n🪙 <b>+{p['amount']} coin</b>", reply_markup=kb_main_menu())
    else:
        exp = await db_grant_premium(uid, "promo", p["amount"])
        await db_log(uid, "promo_redeem", f"{p['code']} premium={p['amount']}k")
        await m.answer(f"🎉 Promokod qabul qilindi!\n⭐ Premium <b>{p['amount']} kunga</b> yoqildi.\n"
                       f"📅 {fmt_date(exp)} gacha", reply_markup=kb_main_menu())


# ───────────────────────── kod / qidiruv holatlari ─────────────────────────
@user_router.message(UserStates.search, F.text)
async def do_search(m: Message):
    q = m.text.strip()
    if len(q) < 2:
        await m.answer("❗ Kamida 2 ta belgi kiriting.")
        return
    rows = await db_search_movies(q)
    if not rows:
        await m.answer("❌ Hech narsa topilmadi.")
        return
    await m.answer(f"🔎 <b>Topilganlar ({len(rows)}):</b>", reply_markup=kb_movies_list_kb(rows))


@user_router.message(UserStates.code, F.text)
async def do_code(m: Message, bot: Bot):
    await deliver_movie(bot, m.from_user.id, m.text.strip())


# ───────────────────────── botni bloklash/qayta ochish ─────────────────────────
@user_router.my_chat_member()
async def on_member_update(ev: ChatMemberUpdated):
    if ev.chat.type == "private":
        await db_set_left(ev.from_user.id, ev.new_chat_member.status in ("kicked", "left"))


# ───────────────────────── fallback: holatsiz matn ─────────────────────────
@user_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def fallback_text(m: Message, bot: Bot):
    txt = m.text.strip()
    if await db_get_movie_by_code(txt):
        await deliver_movie(bot, m.from_user.id, txt)
        return
    rows = await db_search_movies(txt) if len(txt) >= 2 else []
    if rows:
        await m.answer(f"🔎 <b>Topilganlar ({len(rows)}):</b>", reply_markup=kb_movies_list_kb(rows))
    else:
        await m.answer("❌ Bunday kod topilmadi.")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════
def setup_logging() -> None:
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    handlers = [logging.StreamHandler(),
                RotatingFileHandler("bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


async def on_error(event: ErrorEvent) -> bool:
    logging.getLogger("main").exception("Handler xatosi: %s", event.exception)
    upd = event.update
    try:
        if upd.message:
            await upd.message.answer("⚠️ Xatolik yuz berdi. Qaytadan urinib ko'ring.")
        elif upd.callback_query:
            await upd.callback_query.answer("⚠️ Xatolik yuz berdi.", show_alert=True)
    except Exception:
        pass
    return True


async def main() -> None:
    setup_logging()
    log = logging.getLogger("main")
    await db_init_db()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.errors.register(on_error)
    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)

    # Tartib muhim: common → admin → user
    dp.include_router(common_router)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.set_my_commands([BotCommand(command="start", description="Botni ishga tushirish")])
    me = await bot.get_me()
    log.info("Bot ishga tushdi: @%s", me.username)

    task = asyncio.create_task(background_loop(bot))
    task2 = asyncio.create_task(delete_loop(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        task2.cancel()
        await db_close_db()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
