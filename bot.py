import os
import time
import threading
import asyncio
import json
import aiosqlite
import requests
from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, CommandHandler
)
from telegram.error import Forbidden, BadRequest, Conflict

# =========================
# UPTIME / CONFIG
# =========================
START_TIME = time.time()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1003294631521"))
PUBLIC_GROUP_ID = int(os.getenv("PUBLIC_GROUP_ID", "-1003245719893"))

PORT = int(os.getenv("PORT", "10000"))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "https://accidentsfrancebot.onrender.com")

DB_NAME = os.getenv("DB_PATH", "bot_storage.db")

SPAM_COOLDOWN = 4
MUTE_THRESHOLD = 3
MUTE_DURATION_SEC = 300
MUTE_DURATION_SPAM_SUBMISSION = 3600  # 1h

CLEAN_MAX_AGE_PENDING = 3600 * 24
CLEAN_MAX_AGE_ALBUMS = 60
CLEAN_MAX_AGE_SPAM = 3600
CLEAN_MAX_AGE_ARCHIVE = 3600 * 24 * 3  # 3j

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 30
RESTART_MIN_SLEEP_SEC = 3
RESTART_MAX_SLEEP_SEC = 60

# Compat alias
RESTART_BACKOFF_MIN_SEC = RESTART_MIN_SLEEP_SEC
RESTART_BACKOFF_MAX_SEC = RESTART_MAX_SLEEP_SEC

PUBLIC_TOPIC_VIDEOS_ID = 224
PUBLIC_TOPIC_RADARS_ID = 222
PUBLIC_TOPIC_GENERAL_ID = None

# --- Admin notify throttles ---
ADMIN_NOTIFY_COOLDOWN_SEC = 300
HEARTBEAT_ALERT_COOLDOWN_SEC = 300
_last_admin_notify_ts = 0.0
_last_heartbeat_alert_ts = 0.0

# --- Link moderation ---
MUTE_LINKS_DURATION_SEC = 600  # 10 min
ALLOWED_TG_USERNAMES = {
    u.strip().lower() for u in (os.getenv("ALLOWED_TG_USERNAMES") or "").split(",") if u.strip()
} or {"accidentsfr", "accidentsfrancebot"}

accident_keywords = [
    "accident", "accrochage", "carambolage", "choc", "collision",
    "crash", "sortie de route", "perte de contrôle", "perdu le contrôle",
    "sorti de la route", "accidenté", "accident grave", "accident mortel",
    "accident léger", "accident autoroute", "accident route", "accident nationale",
    "accident voiture", "accident moto", "accident camion", "accident poids lourd",
    "voiture accidentée", "camion couché", "camion renversé", "choc frontal",
    "tête à queue", "dashcam", "dash cam", "dash-cam", "caméra embarquée",
    "vidéo accident", "impact", "sorti de la chaussée", "frotter", "accrochage léger",
    "freinage d'urgence", "a percuté", "percuté", "collision arrière",
    "route coupée", "bouchon accident", "accident en direct"
]
radar_keywords = [
    "radar", "radar mobile", "radar fixe", "radar flash", "radar de chantier",
    "radar tourelle", "radar embarqué", "radar double sens", "radar chantier",
    "contrôle", "controle", "contrôle routier", "contrôle radar", "contrôle police",
    "contrôle gendarmerie", "contrôle laser", "contrôle mobile",
    "flash", "flashé", "flasher", "laser", "jumelle", "jumelles",
    "police", "gendarmerie", "camion radar", "voiture radar", "banalisée",
    "voiture banalisée", "voiture de police", "véhicule radar", "véhicule banalisé",
    "camion banalisé", "radar caché", "radar planqué", "piège", "contrôle alcootest",
    "alcoolémie", "radar mobile nouvelle génération", "radar en travaux"
]

# =========================
# ÉTAT EN MÉMOIRE
# =========================
LAST_MSG_TIME = {}
SPAM_COUNT = {}
TEMP_ALBUMS = {}
ALREADY_FORWARDED_ALBUMS = set()

# =========================
# BDD
# =========================
async def init_db():
    print("🗃️ Init SQLite…")
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_reports (
                    report_id TEXT PRIMARY KEY,
                    text TEXT,
                    files_json TEXT,
                    created_ts INTEGER,
                    user_name TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS edit_state (
                    chat_id INTEGER PRIMARY KEY,
                    report_id TEXT,
                    prompt_message_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS muted_users (
                    user_id INTEGER PRIMARY KEY,
                    mute_until_ts INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_archive (
                    message_id INTEGER,
                    chat_id INTEGER,
                    media_group_id TEXT,
                    file_id TEXT,
                    file_type TEXT,
                    caption TEXT,
                    timestamp INTEGER,
                    PRIMARY KEY (message_id, chat_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_group_id
                ON media_archive (media_group_id, chat_id);
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_outbox (
                    report_id TEXT,
                    message_id INTEGER,
                    PRIMARY KEY (report_id, message_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS stats_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    ts INTEGER,
                    meta TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS counters (
                    key TEXT PRIMARY KEY,
                    value INTEGER
                )
            """)
            for k in ("published_total","rejected_total","spam_blocked_total","auto_restarts_total"):
                await db.execute("INSERT OR IGNORE INTO counters(key,value) VALUES(?,0)", (k,))
            await db.commit()
        print(f"🗃️ DB ok '{DB_NAME}'")
    except Exception as e:
        print(f"[DB INIT ERR] {e}")
        raise

# ======= OUTILS STATS =======
async def _inc_counter(db, key: str, delta: int = 1):
    try:
        await db.execute("INSERT INTO counters(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = value + ?",
                         (key, delta, delta))
    except Exception as e:
        print(f"[COUNTER INC {key}] {e}")

async def _get_counter(db, key: str) -> int:
    async with db.execute("SELECT value FROM counters WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def _add_event(db, event_type: str, meta: dict | None = None, ts: int | None = None):
    try:
        await db.execute(
            "INSERT INTO stats_events(event_type, ts, meta) VALUES(?,?,?)",
            (event_type, int(ts or time.time()), json.dumps(meta or {}))
        )
    except Exception as e:
        print(f"[ADD EVENT {event_type}] {e}")

async def _count_events(db, event_type: str, since_ts: int) -> int:
    async with db.execute("SELECT COUNT(*) FROM stats_events WHERE event_type = ? AND ts >= ?", (event_type, since_ts)) as cur:
        row = await cur.fetchone()
        return int(row[0] or 0)

async def _busiest_hour_range_last24(db) -> str | None:
    since = int(time.time()) - 24*3600
    try:
        async with db.execute("""
            SELECT strftime('%H', datetime(ts,'unixepoch','localtime')) AS hh, COUNT(*)
            FROM stats_events
            WHERE event_type='published' AND ts >= ?
            GROUP BY hh
            ORDER BY COUNT(*) DESC
            LIMIT 1
        """, (since,)) as cur:
            row = await cur.fetchone()
            if not row: return None
            start_h = int(row[0])
            end_h = (start_h + 3) % 24
            return f"{start_h}h – {end_h}h"
    except Exception as e:
        print(f"[BUSIEST HOUR] {e}")
        return None

# =========================
# OUTILS
# =========================
def _now() -> float:
    return time.time()

def _is_spam(user_id: int, media_group_id) -> bool:
    if media_group_id:
        return False
    t = _now()
    last = LAST_MSG_TIME.get(user_id, 0)
    if t - last < SPAM_COOLDOWN:
        LAST_MSG_TIME[user_id] = t
        return True
    LAST_MSG_TIME[user_id] = t
    return False

def _make_admin_preview(user_name: str, text: str | None, is_album: bool) -> str:
    head = "📩 Nouveau signalement" + (" (album)" if is_album else "")
    who = f"\n👤 {user_name}"
    body = f"\n\n{text}" if text else ""
    return head + who + body

def _build_mod_keyboard(report_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
            InlineKeyboardButton("✏️ Modifier", callback_data=f"EDIT|{report_id}")
        ],
        [
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}"),
            InlineKeyboardButton("🔇 Rejeter & Muter 1h", callback_data=f"REJECTMUTE|{report_id}")
        ]
    ])

# === Helpers texte/caption & report pipe (UNIFIÉS) ===
def _cap(s: str | None) -> str | None:
    s = (s or "").strip()
    return s if s else None

async def _save_report(report_id: str, text: str | None, files: list[dict], user_name: str):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR REPLACE INTO pending_reports (report_id, text, files_json, created_ts, user_name) VALUES (?,?,?,?,?)",
                (report_id, (text or ""), json.dumps(files), int(time.time()), user_name),
            )
            await db.commit()
    except Exception as e:
        print(f"[SAVE_REPORT] {e}")

def _review_queue(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Queue:
    q = context.application.bot_data.get("review_queue")
    if q is None:
        q = asyncio.Queue()
        context.application.bot_data["review_queue"] = q
        if not context.application.bot_data.get("worker_started"):
            asyncio.create_task(worker_loop(context.application))
            context.application.bot_data["worker_started"] = True
    return q

async def _enqueue(context: ContextTypes.DEFAULT_TYPE, report_id: str, text: str | None, files: list[dict], user_name: str, is_album: bool):
    preview = _make_admin_preview(user_name, text, is_album=is_album)
    await _save_report(report_id, text, files, user_name)
    q = _review_queue(context)
    await q.put({"report_id": report_id, "preview_text": preview, "files": files})

# ===== Helpers liens =====
def _extract_entities_text(msg) -> tuple[list, list]:
    ents = getattr(msg, "entities", []) or []
    cents = getattr(msg, "caption_entities", []) or []
    return ents, cents

def _has_disallowed_link(msg) -> bool:
    text = (msg.caption or msg.text or "")
    ents, cents = _extract_entities_text(msg)
    allents = list(ents) + list(cents)

    for e in allents:
        et = e.type
        if et in ("url", "text_link"):
            url = getattr(e, "url", None)
            if not url and et == "url":
                try:
                    url = text[e.offset:e.offset+e.length]
                except Exception:
                    url = None
            if not url:
                continue
            u = url.strip().lower()

            if not ("t.me/" in u or "telegram.me/" in u or u.startswith("tg://")):
                return True  # external link blocked

            username = None
            if "t.me/" in u or "telegram.me/" in u:
                try:
                    after = u.replace("https://", "").replace("http://", "")
                    after = after.split("t.me/")[-1].split("telegram.me/")[-1]
                    after = after.split("?")[0].split("#")[0]
                    after = after.strip("/").split("/")[0]
                    username = after.lstrip("@").lower() if after else None
                except Exception:
                    username = None
            elif u.startswith("tg://") and "domain=" in u:
                try:
                    qs = u.split("domain=", 1)[1]
                    username = qs.split("&")[0].split("#")[0].split("/")[0].lstrip("@").lower()
                except Exception:
                    username = None

            if username and username.lower() in ALLOWED_TG_USERNAMES:
                continue
            return True

        elif et == "mention":
            try:
                mention = text[e.offset:e.offset+e.length]
            except Exception:
                mention = ""
            username = mention.lstrip("@").lower()
            if username not in ALLOWED_TG_USERNAMES:
                return True

    # fallback brut
    t = text.lower()
    if "http://" in t or "https://" in t or "www." in t or "t.me/" in t or "telegram.me/" in t:
        for allowed in ALLOWED_TG_USERNAMES:
            if f"t.me/{allowed.lower()}" in t or f"telegram.me/{allowed.lower()}" in t or f"@{allowed.lower()}" in t:
                return False
        return True

    return False

async def delete_after_delay(messages: list, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    for msg in messages:
        if not msg:
            continue
        try:
            await msg.delete()
        except (Forbidden, BadRequest):
            pass
        except Exception as e:
            print(f"[DELETE_AFTER_DELAY] {e}")

# --- Admin outbox ---
async def admin_outbox_delete(report_id: str, bot):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT message_id FROM admin_outbox WHERE report_id = ?", (report_id,))
                rows = await c.fetchall()
            for (mid,) in rows:
                try:
                    await bot.delete_message(chat_id=ADMIN_GROUP_ID, message_id=mid)
                except Exception:
                    pass
            await db.execute("DELETE FROM admin_outbox WHERE report_id = ?", (report_id,))
            await db.commit()
    except Exception as e:
        print(f"[ADMIN OUTBOX DELETE] {e}")

async def admin_outbox_track(report_id: str, message_ids: list[int]):
    if not message_ids:
        return
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO admin_outbox (report_id, message_id) VALUES (?, ?)",
                [(report_id, mid) for mid in message_ids]
            )
            await db.commit()
    except Exception as e:
        print(f"[ADMIN OUTBOX TRACK] {e}")

# Outil pour vérifier les admins (avec cache)
async def is_user_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if user_id in [1087968824, 136817688]:
        return True
    cache_key = f"admin_cache_{chat_id}"
    cache_duration = 300
    now = _now()
    if cache_key in context.bot_data:
        admin_ids, timestamp = context.bot_data[cache_key]
        if now - timestamp < cache_duration:
            return user_id in admin_ids
    try:
        admins_list = await context.bot.get_chat_administrators(chat_id)
        admin_ids = {admin.user.id for admin in admins_list}
        context.bot_data[cache_key] = (admin_ids, now)
        return user_id in admin_ids
    except Exception as e:
        print(f"[IS_USER_ADMIN] Erreur API: {e}")
        return False

# ==== Safe user-id extraction ====
def _extract_user_id_from_report_id(report_id: str) -> int | None:
    try:
        head = report_id.split("_", 1)[0]
        return int(head) if head.isdigit() else None
    except Exception:
        return None

# =========================
# HANDLER /start (MP)
# =========================
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "Bonjour ! Je suis le bot officiel de @AccidentsFR.\n\n"
        "🤫 Toutes vos soumissions ici sont 100% ANONYMES.\n\n"
        "Envoyez-moi simplement vos photos, vidéos, ou infos (radars, accidents, contrôles).\n\n"
        "Ajoutez un petit texte pour le contexte, un admin validera avant publication."
    )
    try:
        await update.message.reply_text(welcome)
    except Exception as e:
        print(f"[START] Erreur envoi message: {e}")

# =========================
# WATCHDOG / HEARTBEAT
# =========================
async def heartbeat_loop(application: Application):
    global _last_heartbeat_alert_ts
    failures = 0
    while True:
        await asyncio.sleep(45)
        try:
            await application.bot.get_me()
            failures = 0
        except Exception as e:
            failures += 1
            print(f"[HEARTBEAT] échec {failures}/3 : {e}")
            if failures >= 3:
                now = _now()
                if now - _last_heartbeat_alert_ts >= HEARTBEAT_ALERT_COOLDOWN_SEC:
                    try:
                        await application.bot.send_message(
                            chat_id=ADMIN_GROUP_ID,
                            text="🔴 Connexion Telegram perdue. Redémarrage automatique…"
                        )
                        _last_heartbeat_alert_ts = now
                    except Exception:
                        pass
                try:
                    await application.stop()
                    await application.shutdown()
                except Exception:
                    pass
                return

# =========================
# HANDLER MESSAGES USER
# =========================
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # 1) Nettoyage messages de service
    if (
        msg.new_chat_members or msg.left_chat_member or msg.new_chat_photo
        or msg.delete_chat_photo or msg.new_chat_title
    ):
        if msg.chat_id in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID):
            try:
                await msg.delete()
                return
            except Exception:
                pass
        return

    # 2) Contexte
    user = msg.from_user
    chat_id = msg.chat_id
    media_group_id = msg.media_group_id
    now_ts = _now()

    # 3) Mute en privé
    if chat_id == user.id:
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.cursor() as cursor:
                    await cursor.execute("SELECT mute_until_ts FROM muted_users WHERE user_id = ?", (user.id,))
                    row = await cursor.fetchone()
                if row:
                    mute_until_ts = row[0]
                    now = int(now_ts)
                    if now < mute_until_ts:
                        remaining_min = (mute_until_ts - now) // 60 + 1
                        await msg.reply_text(
                            f"❌ Vous avez été restreint d'envoyer des signalements pour spam.\nTemps restant : {remaining_min} minutes."
                        )
                        return
                    else:
                        await db.execute("DELETE FROM muted_users WHERE user_id = ?", (user.id,))
                        await db.commit()
        except Exception as e:
            print(f"[CHECK MUTE] {e}")

    # 4) Anti-spam groupe public
    is_spam = False
    if chat_id == PUBLIC_GROUP_ID:
        text_raw = (msg.text or msg.caption or "").strip()
        text = text_raw.lower()
        user_state = SPAM_COUNT.get(user.id, {"count": 0, "last": 0})
        flood = _is_spam(user.id, media_group_id)
        gibberish = False
        if len(text) >= 12:
            consonnes = sum(1 for c in text if c in "bcdfghjklmnpqrstvwxyz")
            voyelles = sum(1 for c in text if c in "aeiouy")
            ratio = consonnes / (voyelles + 1)
            if ratio > 5:
                gibberish = True
        is_spam = flood or gibberish
        if is_spam:
            try:
                await msg.delete()
            except Exception as e:
                print(f"[ANTISPAM] delete fail: {e}")
            try:
                async with aiosqlite.connect(DB_NAME) as db:
                    await _inc_counter(db, "spam_blocked_total", 1)
                    await _add_event(db, "spam_blocked")
                    await db.commit()
            except Exception as e:
                print(f"[SPAM STATS] {e}")
            if now_ts - user_state["last"] > 10:
                user_state["count"] = 0
            user_state["count"] += 1
            user_state["last"] = now_ts
            SPAM_COUNT[user.id] = user_state
            if user_state["count"] >= MUTE_THRESHOLD:
                SPAM_COUNT[user.id] = {"count": 0, "last": now_ts}
                until_ts = int(now_ts + MUTE_DURATION_SEC)
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=PUBLIC_GROUP_ID,
                        user_id=user.id,
                        permissions=ChatPermissions(
                            can_send_messages=False,
                            can_send_audios=False,
                            can_send_documents=False,
                            can_send_photos=False,
                            can_send_videos=False,
                            can_send_video_notes=False,
                            can_send_voice_notes=False,
                            can_send_polls=False,
                            can_send_other_messages=False,
                            can_add_web_page_previews=False,
                            can_invite_users=False,
                            can_change_info=False,
                            can_pin_messages=False,
                        ),
                        until_date=until_ts
                    )
                except Exception as e:
                    print(f"[ANTISPAM] mute fail: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_GROUP_ID,
                        text=f"🔇 {user.id} mute {MUTE_DURATION_SEC//60} min pour spam."
                    )
                except Exception as e:
                    print(f"[ANTISPAM] admin notify fail: {e}")
            return

    # 4-bis) Modération des liens (PUBLIC)
    if chat_id == PUBLIC_GROUP_ID:
        try:
            is_admin_user = await is_user_admin(context, PUBLIC_GROUP_ID, user.id)
        except Exception:
            is_admin_user = False

        if not is_admin_user and _has_disallowed_link(msg):
            try:
                await msg.delete()
            except Exception as e:
                print(f"[LINK MOD] delete fail: {e}")

            until_ts = int(_now() + MUTE_LINKS_DURATION_SEC)
            try:
                await context.bot.restrict_chat_member(
                    chat_id=PUBLIC_GROUP_ID,
                    user_id=user.id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_audios=False,
                        can_send_documents=False,
                        can_send_photos=False,
                        can_send_videos=False,
                        can_send_video_notes=False,
                        can_send_voice_notes=False,
                        can_send_polls=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                        can_invite_users=False,
                        can_change_info=False,
                        can_pin_messages=False,
                    ),
                    until_date=until_ts
                )
            except Exception as e:
                print(f"[LINK MOD] mute fail: {e}")

            try:
                mins = MUTE_LINKS_DURATION_SEC // 60
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"🚫 Les liens externes sont interdits ici. Seuls @AccidentsFR et @AccidentsFranceBot sont autorisés.\nMute {mins} min."
                )
            except Exception:
                pass

            try:
                note = await context.bot.send_message(
                    chat_id=ADMIN_GROUP_ID,
                    text=f"🔗 Lien bloqué + mute 10min — user {user.id}"
                )
                asyncio.create_task(delete_after_delay([note], 5))
            except Exception:
                pass

            return

    # 5) Archivage médias (admin + public)
    if (chat_id in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID)) and (msg.photo or msg.video):
        if not is_spam:
            media_type = "video" if msg.video else "photo"
            file_id = msg.video.file_id if msg.video else msg.photo[-1].file_id
            caption = msg.caption or ""
            now_ts2 = _now()
            try:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        """
                        INSERT OR REPLACE INTO media_archive
                        (message_id, chat_id, media_group_id, file_id, file_type, caption, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (msg.message_id, chat_id, media_group_id, file_id, media_type, caption, int(now_ts2))
                    )
                    await db.commit()
            except Exception as e:
                print(f"[ARCHIVE DB] {e}")
        return

    # 6) Ignorer texte non-commande dans les groupes
    if chat_id == PUBLIC_GROUP_ID:
        return
    if chat_id == ADMIN_GROUP_ID:
        return

    # 7) Traitement privé (soumissions)
    if _is_spam(user.id, media_group_id):
        try:
            await msg.reply_text("⏳ Doucement, envoie pas tout d'un coup 🙏")
        except Exception:
            pass
        return

    user_name = f"@{user.username}" if user.username else "anonyme"
    piece_text = (msg.caption or msg.text or "").strip()

    media_type = None
    file_id = None
    if msg.video:
        media_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        media_type = "photo"
        file_id = msg.photo[-1].file_id

    # -- pas album --
    if media_group_id is None:
        report_id = f"{chat_id}_{msg.message_id}"
        files_list = []
        if media_type and file_id:
            files_list.append({"type": media_type, "file_id": file_id})

        await _enqueue(context, report_id, _cap(piece_text), files_list, user_name, is_album=False)
        try:
            await msg.reply_text("✅ Reçu. Vérif avant publication (anonyme).")
        except Exception:
            pass
        return

    # -- album --
    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        TEMP_ALBUMS[media_group_id] = {
            "files": [], "text": piece_text, "user_name": user_name,
            "chat_id": chat_id, "ts": _now(), "done": False,
        }
        album = TEMP_ALBUMS[media_group_id]

    if media_type and file_id:
        album["files"].append({"type": media_type, "file_id": file_id})
    if piece_text and not album.get("text"):
        album["text"] = piece_text
    album["ts"] = _now()
    asyncio.create_task(finalize_album_later(media_group_id, context))

async def finalize_album_later(media_group_id, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(0.5)
    album = TEMP_ALBUMS.get(media_group_id)
    if album is None or album["done"]:
        return
    album["done"] = True
    report_id = f"{album['chat_id']}_{media_group_id}"
    ALREADY_FORWARDED_ALBUMS.add(report_id)
    files_list = album["files"]
    report_text = album["text"]
    user_name = album["user_name"]
    try:
        await _enqueue(context, report_id, _cap(report_text), files_list, user_name, is_album=True)
    except Exception as e:
        print(f"[ALBUM ENQUEUE ERR] {e}")
        return
    try:
        await context.bot.send_message(
            chat_id=album["chat_id"],
            text="✅ Reçu (album). Vérif avant publication."
        )
    except Exception:
        pass
    TEMP_ALBUMS.pop(media_group_id, None)

# =========================
# ADMIN
# =========================
async def send_report_to_admin(application: Application, report_id: str, preview_text: str, files: list[dict]):
    kb = _build_mod_keyboard(report_id)
    sent_ids = []

    # Récup caption depuis DB
    caption_text = None
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT text FROM pending_reports WHERE report_id = ?", (report_id,)) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    caption_text = _cap(row[0])
    except Exception as e:
        print(f"[ADMIN SEND] caption fetch err: {e}")

    try:
        # message d’aperçu
        m = await application.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=preview_text,
            reply_markup=kb,
        )
        sent_ids.append(m.message_id)

        # médias
        if files:
            if len(files) == 1:
                f = files[0]
                if f["type"] == "photo":
                    pm = await application.bot.send_photo(
                        chat_id=ADMIN_GROUP_ID,
                        photo=f["file_id"],
                        caption=caption_text
                    )
                else:
                    pm = await application.bot.send_video(
                        chat_id=ADMIN_GROUP_ID,
                        video=f["file_id"],
                        caption=caption_text
                    )
                sent_ids.append(pm.message_id)
            else:
                media_group = []
                for i, f in enumerate(files):
                    cap = caption_text if i == 0 else None
                    if f["type"] == "photo":
                        media_group.append(InputMediaPhoto(media=f["file_id"], caption=cap))
                    else:
                        media_group.append(InputMediaVideo(media=f["file_id"], caption=cap))
                msgs = await application.bot.send_media_group(
                    chat_id=ADMIN_GROUP_ID,
                    media=media_group
                )
                sent_ids.extend([x.message_id for x in msgs])

        await admin_outbox_track(report_id, sent_ids)

    except Exception as e:
        print(f"[ADMIN SEND] {e}")

async def handle_admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat_id
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT report_id, prompt_message_id FROM edit_state WHERE chat_id = ?", (chat_id,))
                row = await c.fetchone()
            if not row:
                return
            report_id, prompt_message_id = row

            new_text = msg.text or ""
            await db.execute("UPDATE pending_reports SET text = ? WHERE report_id = ?", (new_text, report_id))
            await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
            await db.commit()

            async with db.cursor() as c2:
                await c2.execute("SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?", (report_id,))
                row2 = await c2.fetchone()
            if not row2:
                sent = await msg.reply_text("Erreur : signalement introuvable après MAJ.")
                asyncio.create_task(delete_after_delay([msg, sent], 5))
                return

            text, files_json, user_name = row2
            files = json.loads(files_json)

        await admin_outbox_delete(report_id, context.bot)

        preview_text = _make_admin_preview(user_name, text, is_album=len(files) > 1)
        await send_report_to_admin(context.application, report_id, preview_text, files)

        sent_confirmation = await msg.reply_text("✅ Texte mis à jour.")
        asyncio.create_task(delete_after_delay([msg, sent_confirmation], 5))
        if prompt_message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
            except Exception:
                pass

    except Exception as e:
        print(f"[HANDLE ADMIN EDIT] {e}")
        try:
            sent = await msg.reply_text(f"Erreur MAJ : {e}")
            asyncio.create_task(delete_after_delay([msg, sent], 8))
        except Exception:
            pass

async def handle_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT prompt_message_id FROM edit_state WHERE chat_id = ?", (chat_id,))
                row = await c.fetchone()
            if row:
                prompt_message_id = row[0]
                await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
                await db.commit()
                sent = await msg.reply_text("Modification annulée.")
                await msg.delete()
                asyncio.create_task(delete_after_delay([sent], 5))
                if prompt_message_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
                    except Exception:
                        pass
            else:
                sent = await msg.reply_text("Vous n'étiez pas en train de modifier un message.")
                asyncio.create_task(delete_after_delay([msg, sent], 5))
    except Exception as e:
        print(f"[HANDLE ADMIN CANCEL] {e}")

# =========================
# DASHBOARD
# =========================
async def handle_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT COUNT(*) FROM pending_reports")
                pending_count = (await c.fetchone())[0]
                await c.execute("SELECT COUNT(*) FROM muted_users WHERE mute_until_ts > ?", (int(_now()),))
                muted_count = (await c.fetchone())[0]
                await c.execute("SELECT COUNT(*) FROM edit_state")
                edit_count = (await c.fetchone())[0]

            published_total = await _get_counter(db, "published_total")
            rejected_total = await _get_counter(db, "rejected_total")
            spam_total = await _get_counter(db, "spam_blocked_total")
            auto_restarts_total = await _get_counter(db, "auto_restarts_total")

            since_24h = int(time.time()) - 24*3600
            published_24h = await _count_events(db, "published", since_24h)
            albums_24h = await _count_events(db, "album_received", since_24h)
            spam_24h = await _count_events(db, "spam_blocked", since_24h)
            busiest = await _busiest_hour_range_last24(db)

            async with db.execute("SELECT value FROM bot_state WHERE key='last_restart_ts'") as cur:
                row = await cur.fetchone()
                last_restart_ts = int(row[0]) if row else None
            async with db.execute("SELECT value FROM bot_state WHERE key='last_crash_ts'") as cur:
                row = await cur.fetchone()
                last_crash_ts = int(row[0]) if row else None

        member_count = await context.bot.get_chat_member_count(PUBLIC_GROUP_ID)
        member_count = max(0, member_count - 2)

        uptime_seconds = int(time.time() - START_TIME)
        m, s = divmod(uptime_seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        uptime_str = f"{d}j {h}h {m}m"
        edit_status = "🟢 Non" if edit_count == 0 else "🛑 Oui"

        rej_pct = 0.0
        if published_total + rejected_total > 0:
            rej_pct = (rejected_total * 100.0) / (published_total + rejected_total)

        def fmt_ts(ts):
            return time.strftime('%d/%m %H:%M', time.localtime(ts)) if ts else "—"

        text = (
f"📊 <b>𝘿𝘼𝙎𝙃𝘽𝙊𝘼𝙍𝘿 — AccidentsFR Bot</b>\n"
f"─────────────────────────────\n"
f"🟢 <b>État :</b> En ligne\n"
f"⏱️ <b>Uptime :</b> {uptime_str}\n"
f"♻️ <b>Dernier redémarrage auto :</b> {fmt_ts(last_restart_ts)}\n\n"
f"📌 <b>Modération</b>\n"
f"• <b>Signalements en attente :</b> {pending_count}\n"
f"• <b>Publiés :</b> {published_total}   |   <b>Rejetés :</b> {rejected_total} ({rej_pct:.1f} %)\n"
f"• <b>Utilisateurs mutés :</b> {muted_count}\n"
f"• <b>Édition en cours :</b> {edit_status}\n\n"
f"📌 <b>Activité</b>\n"
f"• <b>Membres (groupe public) :</b> {member_count}\n"
f"• <b>Signalements validés (24h) :</b> {published_24h}\n"
f"• <b>Albums reçus (24h) :</b> {albums_24h}\n"
f"• <b>Heure la + active :</b> {busiest or '—'}\n\n"
f"📌 <b>Système & Sécurité</b>\n"
f"• <b>Redémarrages automatiques :</b> {auto_restarts_total}\n"
f"• <b>Dernier crash détecté :</b> {fmt_ts(last_crash_ts)} (auto-recover)\n"
f"• <b>Anti-spam :</b> {spam_24h} bloqués (24h) / total {spam_total}\n\n"
f"💡 <i>Ce message s’efface dans 60s.</i>"
        )

        sent = await msg.reply_text(text, parse_mode=ParseMode.HTML)
        asyncio.create_task(delete_after_delay([msg, sent], 60))
    except Exception as e:
        print(f"[DASHBOARD] {e}")
        try:
            sent = await msg.reply_text(f"Erreur dashboard : {e}")
            asyncio.create_task(delete_after_delay([msg, sent], 10))
        except Exception:
            pass

# =========================
# /DEPLACER (ADMIN -> PUBLIC)
# =========================
async def handle_deplacer_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    original_msg = msg.reply_to_message
    if not original_msg:
        try:
            m = await msg.reply_text("Usage: répondez à votre message avec /deplacer pour le publier.")
            asyncio.create_task(delete_after_delay([msg, m], 6))
        except Exception: pass
        return

    media_group_id = original_msg.media_group_id
    text_to_analyze = (original_msg.text or original_msg.caption or "").strip()
    text_lower = text_to_analyze.lower()
    target_thread_id = PUBLIC_TOPIC_GENERAL_ID
    if any(word in text_lower for word in accident_keywords):
        target_thread_id = PUBLIC_TOPIC_VIDEOS_ID
    elif any(word in text_lower for word in radar_keywords):
        target_thread_id = PUBLIC_TOPIC_RADARS_ID

    try:
        if media_group_id:
            album_items, album_caption, message_ids_to_delete = [], "", []
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.cursor() as c:
                    await c.execute(
                        "SELECT message_id, file_type, file_id, caption FROM media_archive WHERE media_group_id = ? AND chat_id = ? ORDER BY message_id",
                        (media_group_id, ADMIN_GROUP_ID)
                    )
                    rows = await c.fetchall()
            if not rows:
                raise Exception("Album non trouvé dans l'archive admin.")
            for _, _, _, caption in rows:
                if caption:
                    album_caption = caption
                    break
            for i, (msg_id, file_type, file_id, _) in enumerate(rows):
                message_ids_to_delete.append(msg_id)
                current_caption = album_caption if i == 0 else None
                if file_type == 'photo':
                    album_items.append(InputMediaPhoto(media=file_id, caption=current_caption))
                elif file_type == 'video':
                    album_items.append(InputMediaVideo(media=file_id, caption=current_caption))
            await context.bot.send_media_group(
                chat_id=PUBLIC_GROUP_ID, media=album_items, message_thread_id=target_thread_id
            )
            for msg_id in message_ids_to_delete:
                try:
                    await context.bot.delete_message(ADMIN_GROUP_ID, msg_id)
                except Exception as e:
                    print(f"[DEPLACER_ADMIN] del {msg_id}: {e}")
        else:
            photo = original_msg.photo[-1].file_id if original_msg.photo else None
            video = original_msg.video.file_id if original_msg.video else None
            if photo:
                await context.bot.send_photo(
                    chat_id=PUBLIC_GROUP_ID, photo=photo,
                    caption=text_to_analyze, message_thread_id=target_thread_id
                )
            elif video:
                await context.bot.send_video(
                    chat_id=PUBLIC_GROUP_ID, video=video,
                    caption=text_to_analyze, message_thread_id=target_thread_id
                )
            elif text_to_analyze:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID, text=text_to_analyze,
                    message_thread_id=target_thread_id
                )
            else:
                m = await msg.reply_text("Type non supporté.")
                asyncio.create_task(delete_after_delay([msg, m], 6))
                return
            await original_msg.delete()

        m = await msg.reply_text("✅ Message publié dans le groupe public.")
        asyncio.create_task(delete_after_delay([msg, m], 5))

        try:
            async with aiosqlite.connect(DB_NAME) as db:
                await _inc_counter(db, "published_total", 1)
                await _add_event(db, "published", {"source": "admin_move"})
                await db.commit()
        except Exception as e:
            print(f"[PUBLISH STATS (admin move)] {e}")

    except Exception as e:
        print(f"[DEPLACER_ADMIN] {e}")
        try:
            m = await msg.reply_text(f"Erreur publication : {e}")
            asyncio.create_task(delete_after_delay([msg, m], 8))
        except Exception: pass

# =========================
# /DEPLACER (PUBLIC -> bon topic)
# =========================
async def handle_deplacer_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    try:
        user_id = msg.from_user.id
        is_admin_check_passed = False
        if user_id in [1087968824, 136817688]:
            is_admin_check_passed = True
        else:
            is_admin_check_passed = await is_user_admin(context, PUBLIC_GROUP_ID, user_id)

        if not is_admin_check_passed:
            try:
                await msg.delete()
            except Exception: pass
            return
    except Exception as e:
        print(f"[DEPLACER CHECK] {e}")
        return

    original_msg = msg.reply_to_message
    if not original_msg:
        try:
            m = await msg.reply_text("Usage: répondez à un message avec /deplacer")
            asyncio.create_task(delete_after_delay([msg, m], 6))
        except Exception: pass
        return

    media_group_id = original_msg.media_group_id
    text_to_analyze = (original_msg.text or original_msg.caption or "").strip()
    text_lower = text_to_analyze.lower()
    target_thread_id = PUBLIC_TOPIC_GENERAL_ID
    if any(word in text_lower for word in accident_keywords):
        target_thread_id = PUBLIC_TOPIC_VIDEOS_ID
    elif any(word in text_lower for word in radar_keywords):
        target_thread_id = PUBLIC_TOPIC_RADARS_ID

    if original_msg.message_thread_id == target_thread_id:
        try:
            m = await msg.reply_text("Déjà dans le bon topic.")
            asyncio.create_task(delete_after_delay([m, msg], 4))
        except Exception:
            pass
        return

    try:
        if media_group_id:
            album_items, album_caption, message_ids_to_delete = [], "", []
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.cursor() as c:
                    await c.execute(
                        "SELECT message_id, file_type, file_id, caption FROM media_archive WHERE media_group_id = ? AND chat_id = ? ORDER BY message_id",
                        (media_group_id, PUBLIC_GROUP_ID)
                    )
                    rows = await c.fetchall()
            if not rows:
                raise Exception("Album non trouvé (ou trop vieux). Déplacement simple.")

            for _, _, _, caption in rows:
                if caption:
                    album_caption = caption
                    break
            for i, (msg_id, file_type, file_id, _) in enumerate(rows):
                message_ids_to_delete.append(msg_id)
                current_caption = album_caption if i == 0 else None
                if file_type == 'photo':
                    album_items.append(InputMediaPhoto(media=file_id, caption=current_caption))
                elif file_type == 'video':
                    album_items.append(InputMediaVideo(media=file_id, caption=current_caption))
            await context.bot.send_media_group(
                chat_id=PUBLIC_GROUP_ID, media=album_items, message_thread_id=target_thread_id
            )
            for msg_id in message_ids_to_delete:
                try:
                    await context.bot.delete_message(PUBLIC_GROUP_ID, msg_id)
                except Exception as e:
                    print(f"[DEPLACER] del {msg_id}: {e}")
        else:
            photo = original_msg.photo[-1].file_id if original_msg.photo else None
            video = original_msg.video.file_id if original_msg.video else None
            if photo:
                await context.bot.send_photo(
                    chat_id=PUBLIC_GROUP_ID, photo=photo,
                    caption=text_to_analyze, message_thread_id=target_thread_id
                )
            elif video:
                await context.bot.send_video(
                    chat_id=PUBLIC_GROUP_ID, video=video,
                    caption=text_to_analyze, message_thread_id=target_thread_id
                )
            elif text_to_analyze:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID, text=text_to_analyze,
                    message_thread_id=target_thread_id
                )
            else:
                mm = await msg.reply_text("Type non supporté.")
                asyncio.create_task(delete_after_delay([msg, mm], 6))
                return

        try:
            await original_msg.delete()
        except Exception as e:
            print(f"[DEPLACER PUBLIC] delete source: {e}")
        try:
            await msg.delete()
        except Exception as e:
            print(f"[DEPLACER PUBLIC] delete cmd: {e}")

    except Exception as e:
        print(f"[DEPLACER PUB] {e}")
        try:
            if "Album non trouvé" in str(e) and (original_msg.photo or original_msg.video):
                photo = original_msg.photo[-1].file_id if original_msg.photo else None
                video = original_msg.video.file_id if original_msg.video else None
                if photo:
                    await context.bot.send_photo(chat_id=PUBLIC_GROUP_ID, photo=photo, caption=text_to_analyze, message_thread_id=target_thread_id)
                elif video:
                    await context.bot.send_video(chat_id=PUBLIC_GROUP_ID, video=video, caption=text_to_analyze, message_thread_id=target_thread_id)
                await original_msg.delete()
                await msg.delete()
            else:
                m = await msg.reply_text(f"Erreur déplacement : {e}")
                asyncio.create_task(delete_after_delay([m, msg], 8))
        except Exception:
            pass

# =========================
# /MODIFIER (PUBLIC -> renvoi en modération)
# =========================
async def handle_modifier_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # Admin only
    try:
        user_id = msg.from_user.id
        is_admin_check = (
            user_id in [1087968824, 136817688]
        ) or await is_user_admin(context, PUBLIC_GROUP_ID, user_id)
        if not is_admin_check:
            try:
                await msg.delete()
            except Exception:
                pass
            return
    except Exception as e:
        print(f"[MODIFIER CHECK] {e}")
        return

    original_msg = msg.reply_to_message
    if not original_msg:
        try:
            m = await msg.reply_text("Usage : répondez à un message avec /modifier pour l’envoyer en re-modération.")
            asyncio.create_task(delete_after_delay([msg, m], 6))
        except Exception:
            pass
        return

    media_group_id = original_msg.media_group_id
    override_text = (msg.text or "").replace("/modifier", "").strip()

    try:
        files_list: list[dict] = []
        message_ids_to_delete = []
        base_text = (original_msg.caption or original_msg.text or "").strip()

        # ===== ALBUM =====
        if media_group_id:
            album_caption = None
            rows = []
            try:
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute(
                        """
                        SELECT message_id, file_type, file_id, caption
                        FROM media_archive
                        WHERE media_group_id = ? AND chat_id = ?
                        ORDER BY message_id ASC
                        """,
                        (media_group_id, PUBLIC_GROUP_ID),
                    ) as cur:
                        rows = await cur.fetchall()
            except Exception as e:
                print(f"[MODIFIER album fetch] {e}")

            if rows:
                for mid, file_type, file_id, cap in rows:
                    if not album_caption and cap:
                        album_caption = cap.strip()
                for mid, file_type, file_id, cap in rows:
                    message_ids_to_delete.append(mid)
                    if file_type == "photo":
                        files_list.append({"type": "photo", "file_id": file_id})
                    elif file_type == "video":
                        files_list.append({"type": "video", "file_id": file_id})
            else:
                if original_msg.photo:
                    files_list.append({"type": "photo", "file_id": original_msg.photo[-1].file_id})
                elif original_msg.video:
                    files_list.append({"type": "video", "file_id": original_msg.video.file_id})
                message_ids_to_delete.append(original_msg.message_id)

            final_text = _cap(override_text) or _cap(album_caption) or _cap(base_text)
            if final_text and len(final_text) > 1024:
                final_text = final_text[:1021] + "…"
            if len(files_list) > 10:
                files_list = files_list[:10]

            report_id = f"reedit_{PUBLIC_GROUP_ID}_{media_group_id}_{original_msg.message_id}"

        # ===== MESSAGE SIMPLE =====
        else:
            final_text = _cap(override_text) or _cap(base_text)
            if original_msg.photo:
                files_list.append({"type": "photo", "file_id": original_msg.photo[-1].file_id})
            elif original_msg.video:
                files_list.append({"type": "video", "file_id": original_msg.video.file_id})
            message_ids_to_delete.append(original_msg.message_id)
            report_id = f"reedit_{PUBLIC_GROUP_ID}_{original_msg.message_id}"

        user = original_msg.from_user
        user_name = f"@{user.username}" if user and user.username else "public"

        await _enqueue(context, report_id, final_text, files_list, user_name, is_album=(len(files_list) > 1))

        # cleanup public
        if media_group_id and not message_ids_to_delete:
            message_ids_to_delete.append(original_msg.message_id)
        for mid in set(message_ids_to_delete):
            try:
                await context.bot.delete_message(PUBLIC_GROUP_ID, mid)
            except Exception as e:
                print(f"[MODIFIER] del public {mid}: {e}")
        try:
            await msg.delete()
        except Exception:
            pass

        try:
            info = await context.bot.send_message(
                chat_id=PUBLIC_GROUP_ID,
                text="♻️ Publication retirée — renvoyée en modération.",
            )
            asyncio.create_task(delete_after_delay([info], 5))
        except Exception:
            pass

    except Exception as e:
        print(f"[MODIFIER PUB] {e}")
        try:
            m = await msg.reply_text(f"Erreur /modifier : {e}")
            asyncio.create_task(delete_after_delay([msg, m], 8))
        except Exception:
            pass

# =========================
# BOUTONS
# =========================
async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    action, report_id = data.split("|", 1)
    chat_id = query.message.chat_id

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            try:
                await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
                await db.commit()
            except Exception as e:
                print(f"[BTN CLEAN EDIT_STATE] {e}")

            async with db.cursor() as c:
                await c.execute("SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?", (report_id,))
                row = await c.fetchone()

            if not row:
                try:
                    await admin_outbox_delete(report_id, context.bot)
                except Exception:
                    pass
                return

            info = {
                "text": row[0],
                "files": json.loads(row[1]),
                "user_name": row[2]
            }

            if action == "REJECT":
                m = await context.bot.send_message(ADMIN_GROUP_ID, "❌ Supprimé, non publié.")
                asyncio.create_task(delete_after_delay([m], 5))
                await _inc_counter(db, "rejected_total", 1)
                await _add_event(db, "rejected", {"report_id": report_id})
                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()
                await admin_outbox_delete(report_id, context.bot)
                return

            if action == "REJECTMUTE":
                user_id = _extract_user_id_from_report_id(report_id)
                mute_duration = MUTE_DURATION_SPAM_SUBMISSION
                mute_until_ts = int(_now() + mute_duration)

                if user_id:
                    await db.execute(
                        "INSERT OR REPLACE INTO muted_users (user_id, mute_until_ts) VALUES (?, ?)",
                        (user_id, mute_until_ts)
                    )

                await _inc_counter(db, "rejected_total", 1)
                await _add_event(db, "rejected", {"report_id": report_id, "muted": bool(user_id)})

                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()

                try:
                    if user_id:
                        hours = mute_duration // 3600
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"❌ Votre soumission a été rejetée.\n\nVous avez été restreint d'envoyer de nouveaux signalements pour {hours} heure(s)."
                        )
                except Exception as e:
                    print(f"[NOTIFY USER REJECTMUTE] {e}")

                m = await context.bot.send_message(ADMIN_GROUP_ID, "🔇 Rejeté + mute 1h.")
                asyncio.create_task(delete_after_delay([m], 5))
                await admin_outbox_delete(report_id, context.bot)
                return

            if action == "EDIT":
                current_text = info.get("text", "")
                try:
                    sent_prompt = await context.bot.send_message(
                        chat_id=ADMIN_GROUP_ID,
                        text=f"✏️ **Modification en cours...**\n\n**Texte actuel :**\n`{current_text}`\n\nEnvoyez le nouveau texte. (/cancel pour annuler)",
                        parse_mode="Markdown"
                    )
                    prompt_message_id = sent_prompt.message_id

                    await db.execute(
                        "INSERT OR REPLACE INTO edit_state (chat_id, report_id, prompt_message_id) VALUES (?, ?, ?)",
                        (chat_id, report_id, prompt_message_id)
                    )
                    await db.commit()
                except Exception as e:
                    print(f"[EDIT BUTTON] {e}")
                    if 'sent_prompt' in locals():
                        await sent_prompt.delete()
                    m = await context.bot.send_message(ADMIN_GROUP_ID, "⚠️ Une modification est déjà en cours. /cancel d'abord.")
                    asyncio.create_task(delete_after_delay([m], 8))
                return

            if action == "APPROVE":
                files = info["files"]
                text = _cap(info["text"])
                caption_for_public = text
                text_lower = (text or "").lower()

                if any(word in text_lower for word in accident_keywords):
                    target_thread_id = PUBLIC_TOPIC_VIDEOS_ID
                elif any(word in text_lower for word in radar_keywords):
                    target_thread_id = PUBLIC_TOPIC_RADARS_ID
                else:
                    target_thread_id = PUBLIC_TOPIC_GENERAL_ID

                try:
                    if not files:
                        if text:
                            await context.bot.send_message(
                                chat_id=PUBLIC_GROUP_ID, text=text,
                                message_thread_id=target_thread_id
                            )
                        else:
                            m = await context.bot.send_message(ADMIN_GROUP_ID, "❌ Rien à publier (vide).")
                            asyncio.create_task(delete_after_delay([m], 5))
                            return
                    elif len(files) == 1:
                        f = files[0]
                        if f["type"] == "photo":
                            await context.bot.send_photo(
                                chat_id=PUBLIC_GROUP_ID, photo=f["file_id"],
                                caption=caption_for_public, message_thread_id=target_thread_id
                            )
                        else:
                            await context.bot.send_video(
                                chat_id=PUBLIC_GROUP_ID, video=f["file_id"],
                                caption=caption_for_public, message_thread_id=target_thread_id
                            )
                    else:
                        media_group = []
                        for i, f in enumerate(files):
                            caption = caption_for_public if i == 0 else None
                            if f["type"] == "photo":
                                media_group.append(InputMediaPhoto(media=f["file_id"], caption=caption))
                            else:
                                media_group.append(InputMediaVideo(media=f["file_id"], caption=caption))
                        await context.bot.send_media_group(
                            chat_id=PUBLIC_GROUP_ID, media=media_group,
                            message_thread_id=target_thread_id
                        )

                    # Notify user (safe)
                    try:
                        user_chat_id = _extract_user_id_from_report_id(report_id)
                        if user_chat_id:
                            await context.bot.send_message(
                                chat_id=user_chat_id,
                                text="✅ Ton signalement a été publié dans le canal @AccidentsFR."
                            )
                    except Exception as e:
                        print(f"[NOTIFY USER APPROVE] {e}")

                    await _inc_counter(db, "published_total", 1)
                    await _add_event(db, "published", {"report_id": report_id})

                    await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                    await db.commit()

                    m = await context.bot.send_message(ADMIN_GROUP_ID, "✅ Publié dans le groupe public.")
                    asyncio.create_task(delete_after_delay([m], 5))
                    await admin_outbox_delete(report_id, context.bot)

                except Exception as e:
                    print(f"[PUBLISH ERR] {e}")
                    m = await context.bot.send_message(ADMIN_GROUP_ID, f"⚠️ Erreur publication: {e}")
                    asyncio.create_task(delete_after_delay([m], 8))
                return

    except Exception as e:
        print(f"[ON_BUTTON_CLICK] {e}")

# =========================
# WORKERS
# =========================
async def worker_loop(application: Application):
    print("👷 Worker démarré")
    # utilise la queue centralisée
    q = application.bot_data.get("review_queue")
    if q is None:
        q = asyncio.Queue()
        application.bot_data["review_queue"] = q
    while True:
        try:
            item = await q.get()
            rid = item["report_id"]
            preview = item["preview_text"]
            files = item["files"]
            await send_report_to_admin(application, rid, preview, files)
            q.task_done()
        except Exception as e:
            print(f"[WORKER] {e}")
            await asyncio.sleep(1)

async def cleaner_loop():
    print("🧽 Cleaner démarré")
    while True:
        await asyncio.sleep(60)
        now = _now()
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                cutoff_ts_pending = int(now - CLEAN_MAX_AGE_PENDING)
                await db.execute("DELETE FROM pending_reports WHERE created_ts < ?", (cutoff_ts_pending,))
                cutoff_ts_archive = int(now - CLEAN_MAX_AGE_ARCHIVE)
                await db.execute("DELETE FROM media_archive WHERE timestamp < ?", (cutoff_ts_archive,))
                if int(now) % (6*3600) == 0:
                    await db.execute("DELETE FROM edit_state")
                    await db.execute("DELETE FROM muted_users WHERE mute_until_ts < ?", (int(now),))
                    await db.execute("DELETE FROM admin_outbox")
                await db.commit()

            cutoff_ts_spam = now - CLEAN_MAX_AGE_SPAM
            for uid in list(LAST_MSG_TIME.keys()):
                if LAST_MSG_TIME[uid] < cutoff_ts_spam:
                    LAST_MSG_TIME.pop(uid, None)
            for uid in list(SPAM_COUNT.keys()):
                if SPAM_COUNT[uid]["last"] < cutoff_ts_spam:
                    SPAM_COUNT.pop(uid, None)

            cutoff_ts_albums = now - CLEAN_MAX_AGE_ALBUMS
            for mgid in list(TEMP_ALBUMS.keys()):
                if TEMP_ALBUMS[mgid]["ts"] < cutoff_ts_albums:
                    TEMP_ALBUMS.pop(mgid, None)

        except Exception as e:
            print(f"[CLEANER] {e}")

# =========================
# KEEP ALIVE + Flask
# =========================
def keep_alive():
    while True:
        try:
            requests.get(KEEP_ALIVE_URL, timeout=5)
        except Exception:
            pass
        time.sleep(600)

flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def hello():
    return "OK - bot alive"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# =========================
# LOCK / UNLOCK
# =========================
DEFAULT_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
    can_change_info=False,
    can_pin_messages=False,
)

LOCK_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_invite_users=False,
    can_change_info=False,
    can_pin_messages=False,
)

async def handle_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        try:
            await msg.delete()
        except Exception: pass
        return
    try:
        await context.bot.set_chat_permissions(chat_id=PUBLIC_GROUP_ID, permissions=LOCK_PERMISSIONS)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT value FROM bot_state WHERE key = 'lock_message_id'")
                row = await c.fetchone()
            if row:
                try:
                    await context.bot.delete_message(PUBLIC_GROUP_ID, int(row[0]))
                except Exception: pass
        sent_msg = await context.bot.send_message(
            chat_id=PUBLIC_GROUP_ID,
            text="🔒 Le chat a été temporairement verrouillé par un administrateur."
        )
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ("lock_message_id", str(sent_msg.message_id))
            )
            await db.commit()
        await msg.delete()
    except Exception as e:
        print(f"[LOCK] Erreur: {e}")
        try:
            m = await msg.reply_text(f"Erreur lors du verrouillage: {e}")
            asyncio.create_task(delete_after_delay([msg, m], 10))
        except Exception: pass

async def handle_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        try:
            await msg.delete()
        except Exception: pass
        return
    try:
        await context.bot.set_chat_permissions(chat_id=PUBLIC_GROUP_ID, permissions=DEFAULT_PERMISSIONS)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT value FROM bot_state WHERE key = 'lock_message_id'")
                row = await c.fetchone()
            if row:
                try:
                    await context.bot.delete_message(PUBLIC_GROUP_ID, int(row[0]))
                except Exception: pass
            await db.execute("DELETE FROM bot_state WHERE key = 'lock_message_id'")
            await db.commit()
        sent_msg = await context.bot.send_message(
            chat_id=PUBLIC_GROUP_ID,
            text="🔓 Le chat est déverrouillé."
        )
        await msg.delete()
        asyncio.create_task(delete_after_delay([sent_msg], 5))
    except Exception as e:
        print(f"[UNLOCK] Erreur: {e}")
        try:
            m = await msg.reply_text(f"Erreur lors du déverrouillage: {e}")
            asyncio.create_task(delete_after_delay([msg, m], 10))
        except Exception: pass

async def handle_public_admin_command_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        try:
            await msg.delete()
        except Exception:
            pass

# =========================
# MAIN + AUTO-RESTART
# =========================
async def _post_init(application: Application):
    try:
        await init_db()
        # init queue + worker ici (évite "bound to different event loop")
        if application.bot_data.get("review_queue") is None:
            application.bot_data["review_queue"] = asyncio.Queue()
        if not application.bot_data.get("worker_started"):
            asyncio.create_task(worker_loop(application))
            application.bot_data["worker_started"] = True

        asyncio.create_task(cleaner_loop())
        asyncio.create_task(heartbeat_loop(application))
        try:
            await application.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text="🟢 Bot relancé (polling activé)."
            )
        except Exception:
            pass
    except Exception as e:
        print(f"[POST_INIT] {e}")

def _notify_admin_sync(text: str, *, force: bool = False):
    global _last_admin_notify_ts
    now = _now()
    if not force and (now - _last_admin_notify_ts) < ADMIN_NOTIFY_COOLDOWN_SEC:
        print("[NOTIFY_ADMIN_SYNC] Skipped (cooldown)")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": ADMIN_GROUP_ID, "text": text}
        requests.post(url, data=data, timeout=5)
        _last_admin_notify_ts = now
    except Exception as e:
        print(f"[NOTIFY_ADMIN_SYNC ERR] {e}")

def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    backoff = 2
    while True:
        try:
            app = (ApplicationBuilder()
                   .token(BOT_TOKEN)
                   .post_init(_post_init)
                   .build())

            # Handlers
            app.add_handler(CommandHandler("start", handle_start, filters=filters.ChatType.PRIVATE))

            # Admin room
            app.add_handler(CommandHandler("cancel", handle_admin_cancel, filters=filters.Chat(ADMIN_GROUP_ID)))
            app.add_handler(CommandHandler("dashboard", handle_dashboard, filters=filters.Chat(ADMIN_GROUP_ID)))
            app.add_handler(CommandHandler("deplacer", handle_deplacer_admin, filters=filters.Chat(ADMIN_GROUP_ID) & filters.REPLY))
            app.add_handler(MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND, handle_admin_edit))

            # Public
            app.add_handler(CommandHandler("lock", handle_lock, filters=filters.Chat(PUBLIC_GROUP_ID)))
            app.add_handler(CommandHandler("unlock", handle_unlock, filters=filters.Chat(PUBLIC_GROUP_ID)))
            app.add_handler(CommandHandler("deplacer", handle_deplacer_public, filters=filters.Chat(PUBLIC_GROUP_ID) & filters.REPLY))
            app.add_handler(CommandHandler("modifier", handle_modifier_public, filters=filters.Chat(PUBLIC_GROUP_ID) & filters.REPLY))
            app.add_handler(CommandHandler(["dashboard", "cancel", "deplacer", "modifier"],
                                           handle_public_admin_command_cleanup,
                                           filters=filters.Chat(PUBLIC_GROUP_ID) & ~filters.REPLY))

            # Boutons
            app.add_handler(CallbackQueryHandler(on_button_click))

            # Catch-all
            app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

            print("🚀 Bot démarré, en écoute…")
            try:
                app.run_polling(poll_interval=POLL_INTERVAL, timeout=POLL_TIMEOUT, close_loop=False)
            except Conflict:
                # Un seul polling à la fois — normal au redeploy
                pass

            _notify_admin_sync("🟠 Bot redémarre (watchdog).")
            try:
                asyncio.run(_log_restart())
            except Exception:
                pass
            backoff = 2

        except Exception as e:
            print(f"[MAIN LOOP ERR] {e}")
            _notify_admin_sync(f"🔴 Bot crash détecté. Redémarrage…\n{e}")
            try:
                asyncio.run(_log_crash_and_plan_restart())
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            except Exception:
                pass
            continue

async def _log_restart():
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await _inc_counter(db, "auto_restarts_total", 1)
            await _add_event(db, "restart")
            await db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('last_restart_ts',?)", (str(int(time.time())),))
            await db.commit()
    except Exception as e:
        print(f"[LOG RESTART] {e}")

async def _log_crash_and_plan_restart():
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await _add_event(db, "crash")
            await db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('last_crash_ts',?)", (str(int(time.time())),))
            await db.commit()
    except Exception as e:
        print(f"[LOG CRASH] {e}")

if __name__ == "__main__":
    main()
