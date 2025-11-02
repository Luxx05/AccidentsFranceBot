# ============================================================
#  AccidentsFR BOT (version améliorée - Refactor complet)
# ============================================================
#  ✅ Optimisations incluses :
#     - Logging propre (pas de print)
#     - Gestion DB thread-safe via queue
#     - Endpoint /health (uptime + DB check)
#     - Permissions unifiées via make_permissions()
#     - Anti rate-limit Telegram
#     - Nettoyage silencieux loggé
# ============================================================

import os
import time
import json
import asyncio
import logging
import threading
import requests
import aiosqlite

from flask import Flask, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, CommandHandler
)
from telegram.error import Forbidden, BadRequest, TimedOut, RetryAfter

# ============================================================
# LOGGING SETUP
# ============================================================

LOG_FILE = "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION GLOBALE
# ============================================================

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
CLEAN_MAX_AGE_ARCHIVE = 3600 * 24 * 3  # 3 jours

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 30

PUBLIC_TOPIC_VIDEOS_ID = 224
PUBLIC_TOPIC_RADARS_ID = 222
PUBLIC_TOPIC_GENERAL_ID = None

# Limiteur global pour éviter les RateLimit (5 actions/s max)
TELEGRAM_RATE_LIMITER = asyncio.Semaphore(5)

# ============================================================
# PERMISSIONS CENTRALISÉES
# ============================================================

def make_permissions(locked: bool) -> ChatPermissions:
    """Retourne un objet ChatPermissions configuré selon l’état du chat."""
    return ChatPermissions(
        can_send_messages=not locked,
        can_send_audios=not locked,
        can_send_documents=not locked,
        can_send_photos=not locked,
        can_send_videos=not locked,
        can_send_video_notes=not locked,
        can_send_voice_notes=not locked,
        can_send_polls=not locked,
        can_send_other_messages=not locked,
        can_add_web_page_previews=not locked,
        can_invite_users=not locked,
        can_change_info=False,
        can_pin_messages=False,
    )

# ============================================================
# CLÉS EN MÉMOIRE (ÉTAT COURANT)
# ============================================================

LAST_MSG_TIME = {}
SPAM_COUNT = {}
TEMP_ALBUMS = {}
REVIEW_QUEUE = asyncio.Queue()
ALREADY_FORWARDED_ALBUMS = set()

# ============================================================
# ACCÈS DB VIA QUEUE (SÉCURISÉ)
# ============================================================

DB_QUEUE = asyncio.Queue()

async def db_worker():
    """Thread de gestion unique de la base de données SQLite."""
    log.info("🗃️ Démarrage du thread DB sécurisé…")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        while True:
            query, params, mode, fut = await DB_QUEUE.get()
            try:
                async with db.cursor() as cursor:
                    await cursor.execute(query, params)
                    if mode == "fetchone":
                        result = await cursor.fetchone()
                    elif mode == "fetchall":
                        result = await cursor.fetchall()
                    else:
                        result = None
                if mode == "commit":
                    await db.commit()
                if mode == "vacuum":
                    await db.execute("VACUUM")
                    await db.commit()
                if fut and not fut.cancelled():
                    fut.set_result(result)
            except Exception as e:
                log.error(f"[DB_WORKER] Erreur SQL: {e} | {query}")
                if fut and not fut.cancelled():
                    fut.set_exception(e)
            finally:
                DB_QUEUE.task_done()

async def db_execute(query, params=(), mode="commit"):
    fut = asyncio.get_event_loop().create_future()
    await DB_QUEUE.put((query, params, mode, fut))
    return await fut

# ============================================================
# INITIALISATION DB
# ============================================================

async def init_db():
    log.info("🗃️ Initialisation SQLite…")
    try:
        await db_execute("""
            CREATE TABLE IF NOT EXISTS pending_reports (
                report_id TEXT PRIMARY KEY,
                text TEXT,
                files_json TEXT,
                created_ts INTEGER,
                user_name TEXT
            )
        """)
        await db_execute("""
            CREATE TABLE IF NOT EXISTS edit_state (
                chat_id INTEGER PRIMARY KEY,
                report_id TEXT,
                prompt_message_id INTEGER,
                FOREIGN KEY (report_id) REFERENCES pending_reports(report_id) ON DELETE CASCADE
            )
        """)
        await db_execute("""
            CREATE TABLE IF NOT EXISTS muted_users (
                user_id INTEGER PRIMARY KEY,
                mute_until_ts INTEGER
            )
        """)
        await db_execute("""
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
        await db_execute("CREATE INDEX IF NOT EXISTS idx_media_group_id ON media_archive (media_group_id, chat_id);")
        await db_execute("""
            CREATE TABLE IF NOT EXISTS admin_outbox (
                report_id TEXT,
                message_id INTEGER,
                PRIMARY KEY (report_id, message_id)
            )
        """)
        await db_execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db_execute("commit")
        log.info(f"🗃️ Base initialisée avec succès → {DB_NAME}")
    except Exception as e:
        log.error(f"[DB INIT ERR] {e}")
        raise

# ============================================================
# UTILITAIRES GÉNÉRAUX
# ============================================================

def _now() -> float:
    return time.time()

async def safe_delete_message(bot, chat_id, message_id):
    try:
        async with TELEGRAM_RATE_LIMITER:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (Forbidden, BadRequest):
        pass
    except Exception as e:
        log.warning(f"[DELETE FAIL] {e}")

async def safe_send(bot_method, *args, **kwargs):
    """Sécurise un envoi Telegram avec rate-limit et logs."""
    async with TELEGRAM_RATE_LIMITER:
        try:
            return await bot_method(*args, **kwargs)
        except RetryAfter as e:
            log.warning(f"Rate limit atteint, attente {e.retry_after}s…")
            await asyncio.sleep(e.retry_after)
            return await bot_method(*args, **kwargs)
        except TimedOut:
            log.warning("Timeout Telegram, tentative suivante…")
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"[TELEGRAM SEND FAIL] {e}")
    return None

async def delete_after_delay(messages: list, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    for msg in messages:
        if msg:
            try:
                await msg.delete()
            except Exception as e:
                log.warning(f"[DELETE_AFTER_DELAY] {e}")

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

# ============================================================
# FLASK SERVER (KEEP-ALIVE + HEALTHCHECK)
# ============================================================

flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def hello():
    return "OK - bot alive"

@flask_app.route("/health", methods=["GET"])
def health():
    uptime = int(time.time() - START_TIME)
    try:
        db_ok = os.path.exists(DB_NAME)
        status = {
            "status": "healthy" if db_ok else "db_missing",
            "uptime_sec": uptime,
            "db_file": DB_NAME,
        }
        return jsonify(status), 200 if db_ok else 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def run_flask():
    log.info(f"🌐 Flask démarré sur port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

def keep_alive():
    while True:
        try:
            requests.get(KEEP_ALIVE_URL, timeout=5)
            log.info("🌍 Keep-alive ping envoyé à Render")
        except Exception as e:
            log.warning(f"[KEEP ALIVE FAIL] {e}")
        time.sleep(600)

# ============================================================
# FIN PARTIE 1/3
# ============================================================

# ============================================================
#  Partie 2/3 — Handlers & Workers
# ============================================================

from telegram import ChatPermissions

# ============================================================
# HANDLER /start (MP)
# ============================================================

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "Bonjour ! Je suis le bot officiel de @AccidentsFR.\n\n"
        "🤫 Toutes vos soumissions ici sont 100% ANONYMES.\n\n"
        "Envoyez-moi vos photos, vidéos, ou infos (radars, accidents, contrôles).\n\n"
        "Ajoutez un texte pour le contexte (ex: \"Radar mobile A7, sortie Montélimar\" ou \"Dashcam accident N104\").\n\n"
        "Un admin validera votre signalement avant publication."
    )
    try:
        await safe_send(update.message.reply_text, welcome)
    except Exception as e:
        log.error(f"[START] Erreur d’envoi message: {e}")

# ============================================================
# OUTILS ADMIN / UTILISATEUR
# ============================================================

async def is_user_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Vérifie (avec cache 5min) si un utilisateur est admin du chat."""
    if user_id in [1087968824, 136817688]:
        return True
    cache_key = f"admin_cache_{chat_id}"
    cache_duration = 300
    now = _now()
    if cache_key in context.bot_data:
        admin_ids, ts = context.bot_data[cache_key]
        if now - ts < cache_duration:
            return user_id in admin_ids
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        ids = {adm.user.id for adm in admins}
        context.bot_data[cache_key] = (ids, now)
        return user_id in ids
    except Exception as e:
        log.error(f"[ADMIN CHECK] {e}")
        return False

# ============================================================
# HANDLER MESSAGES UTILISATEURS (PRIVÉS & GROUPES)
# ============================================================

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    chat_id = msg.chat_id
    now_ts = _now()
    media_group_id = msg.media_group_id

    # 1️⃣ Nettoyage messages de service
    if msg.new_chat_members or msg.left_chat_member:
        try:
            await safe_delete_message(context.bot, chat_id, msg.message_id)
        except Exception as e:
            log.warning(f"[SERVICE CLEAN] {e}")
        return

    # 2️⃣ Vérifie mute utilisateur (en MP)
    if chat_id == user.id:
        try:
            row = await db_execute("SELECT mute_until_ts FROM muted_users WHERE user_id = ?", (user.id,), "fetchone")
            if row:
                mute_until_ts = row[0]
                if int(now_ts) < mute_until_ts:
                    remaining = (mute_until_ts - int(now_ts)) // 60 + 1
                    await safe_send(
                        msg.reply_text,
                        f"❌ Vous êtes temporairement restreint pour spam.\nTemps restant : {remaining} minutes."
                    )
                    return
                else:
                    await db_execute("DELETE FROM muted_users WHERE user_id = ?", (user.id,))
        except Exception as e:
            log.error(f"[CHECK MUTE] {e}")

    # 3️⃣ Anti-spam groupe public
    if chat_id == PUBLIC_GROUP_ID:
        text_raw = (msg.text or msg.caption or "").strip()
        text = text_raw.lower()
        flood = _is_spam(user.id, media_group_id)
        gibberish = False
        if len(text) >= 5:
            consonnes = sum(1 for c in text if c in "bcdfghjklmnpqrstvwxyz")
            voyelles = sum(1 for c in text if c in "aeiouy")
            if consonnes / (voyelles + 1) > 5:
                gibberish = True
        if flood or gibberish:
            await safe_delete_message(context.bot, chat_id, msg.message_id)
            user_state = SPAM_COUNT.get(user.id, {"count": 0, "last": 0})
            if now_ts - user_state["last"] > 10:
                user_state["count"] = 0
            user_state["count"] += 1
            user_state["last"] = now_ts
            SPAM_COUNT[user.id] = user_state
            if user_state["count"] >= MUTE_THRESHOLD:
                until_ts = int(now_ts + MUTE_DURATION_SEC)
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=PUBLIC_GROUP_ID,
                        user_id=user.id,
                        permissions=make_permissions(True),
                        until_date=until_ts
                    )
                    log.warning(f"[ANTISPAM] {user.id} muté {MUTE_DURATION_SEC//60}min")
                except Exception as e:
                    log.error(f"[ANTISPAM MUTE] {e}")
            return

    # 4️⃣ Archivage médias (public/admin)
    if (chat_id in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID)) and (msg.photo or msg.video):
        media_type = "video" if msg.video else "photo"
        file_id = msg.video.file_id if msg.video else msg.photo[-1].file_id
        caption = msg.caption or ""
        try:
            await db_execute(
                """
                INSERT OR REPLACE INTO media_archive
                (message_id, chat_id, media_group_id, file_id, file_type, caption, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (msg.message_id, chat_id, media_group_id, file_id, media_type, caption, int(now_ts))
            )
        except Exception as e:
            log.error(f"[ARCHIVE] {e}")
        return

    # 5️⃣ Soumissions privées (hors admin/public)
    if chat_id not in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID):
        if _is_spam(user.id, media_group_id):
            await safe_send(msg.reply_text, "⏳ Doucement, envoie pas tout d'un coup 🙏")
            return

        user_name = f"@{user.username}" if user.username else "anonyme"
        piece_text = (msg.caption or msg.text or "").strip()
        media_type = "video" if msg.video else "photo" if msg.photo else None
        file_id = msg.video.file_id if msg.video else (msg.photo[-1].file_id if msg.photo else None)
        report_id = f"{chat_id}_{msg.message_id if not media_group_id else media_group_id}"
        files_list = [{"type": media_type, "file_id": file_id}] if file_id else []

        # Insertion BDD
        try:
            await db_execute(
                "INSERT INTO pending_reports (report_id, text, files_json, created_ts, user_name) VALUES (?, ?, ?, ?, ?)",
                (report_id, piece_text, json.dumps(files_list), int(_now()), user_name)
            )
            await REVIEW_QUEUE.put({
                "report_id": report_id,
                "preview_text": f"📩 Nouveau signalement\n👤 {user_name}\n\n{piece_text or ''}",
                "files": files_list,
            })
            await safe_send(msg.reply_text, "✅ Reçu. Vérif avant publication (anonyme).")
        except Exception as e:
            log.error(f"[SUBMIT ERROR] {e}")

# ============================================================
# ADMIN: APPROUVE / REJET / EDIT
# ============================================================

async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        action, report_id = query.data.split("|", 1)
    except Exception:
        return

    # Lecture du report
    try:
        row = await db_execute(
            "SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?",
            (report_id,), "fetchone"
        )
        if not row:
            log.warning(f"[BTN] Report {report_id} introuvable.")
            return
        text, files_json, user_name = row
        files = json.loads(files_json)
    except Exception as e:
        log.error(f"[BTN FETCH ERR] {e}")
        return

    if action == "REJECT":
        await db_execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
        await safe_send(context.bot.send_message, ADMIN_GROUP_ID, "❌ Supprimé, non publié.")
        return

    if action == "APPROVE":
        text_lower = text.lower() if text else ""
        if any(w in text_lower for w in accident_keywords):
            topic = PUBLIC_TOPIC_VIDEOS_ID
        elif any(w in text_lower for w in radar_keywords):
            topic = PUBLIC_TOPIC_RADARS_ID
        else:
            topic = PUBLIC_TOPIC_GENERAL_ID

        try:
            if not files:
                await safe_send(context.bot.send_message, PUBLIC_GROUP_ID, text, message_thread_id=topic)
            elif len(files) == 1:
                f = files[0]
                send_method = context.bot.send_photo if f["type"] == "photo" else context.bot.send_video
                await safe_send(send_method, PUBLIC_GROUP_ID, f["file_id"], caption=text, message_thread_id=topic)
            else:
                media = [
                    InputMediaPhoto(media=f["file_id"], caption=text if i == 0 else None)
                    if f["type"] == "photo"
                    else InputMediaVideo(media=f["file_id"], caption=text if i == 0 else None)
                    for i, f in enumerate(files)
                ]
                await safe_send(context.bot.send_media_group, PUBLIC_GROUP_ID, media, message_thread_id=topic)

            await db_execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
            await safe_send(context.bot.send_message, ADMIN_GROUP_ID, "✅ Publié avec succès.")
        except Exception as e:
            log.error(f"[APPROVE ERR] {e}")

# ============================================================
# WORKER LOOP (ENVOI VERS ADMIN)
# ============================================================

async def worker_loop(application: Application):
    log.info("👷 Worker de modération démarré.")
    while True:
        try:
            item = await REVIEW_QUEUE.get()
            rid, preview, files = item["report_id"], item["preview_text"], item["files"]
            try:
                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{rid}"),
                        InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{rid}")
                    ]
                ])
                msg = await safe_send(application.bot.send_message, ADMIN_GROUP_ID, preview, reply_markup=kb)
                if files:
                    if len(files) == 1:
                        f = files[0]
                        if f["type"] == "photo":
                            await safe_send(application.bot.send_photo, ADMIN_GROUP_ID, f["file_id"])
                        else:
                            await safe_send(application.bot.send_video, ADMIN_GROUP_ID, f["file_id"])
                    else:
                        media = [
                            InputMediaPhoto(media=f["file_id"]) if f["type"] == "photo"
                            else InputMediaVideo(media=f["file_id"])
                            for f in files
                        ]
                        await safe_send(application.bot.send_media_group, ADMIN_GROUP_ID, media)
            except Exception as e:
                log.error(f"[WORKER SEND] {e}")
            REVIEW_QUEUE.task_done()
        except Exception as e:
            log.error(f"[WORKER LOOP] {e}")
            await asyncio.sleep(1)

# ============================================================
# CLEANER LOOP (PURGE DB + MÉMOIRE)
# ============================================================

async def cleaner_loop():
    log.info("🧽 Cleaner démarré.")
    while True:
        await asyncio.sleep(60)
        now = _now()
        try:
            await db_execute("DELETE FROM pending_reports WHERE created_ts < ?", (int(now - CLEAN_MAX_AGE_PENDING),))
            await db_execute("DELETE FROM media_archive WHERE timestamp < ?", (int(now - CLEAN_MAX_AGE_ARCHIVE),))
            await db_execute("DELETE FROM muted_users WHERE mute_until_ts < ?", (int(now),))
            if int(now) % 3600 == 0:
                await db_execute("VACUUM", mode="vacuum")

            # Mémoire
            for dct, cutoff in [(LAST_MSG_TIME, CLEAN_MAX_AGE_SPAM), (SPAM_COUNT, CLEAN_MAX_AGE_SPAM)]:
                for uid in list(dct.keys()):
                    if dct[uid] and isinstance(dct[uid], dict):
                        if dct[uid].get("last", 0) < now - cutoff:
                            dct.pop(uid, None)
                    elif dct[uid] < now - cutoff:
                        dct.pop(uid, None)

            for mgid in list(TEMP_ALBUMS.keys()):
                if TEMP_ALBUMS[mgid]["ts"] < now - CLEAN_MAX_AGE_ALBUMS:
                    TEMP_ALBUMS.pop(mgid, None)

        except Exception as e:
            log.error(f"[CLEANER] {e}")
            
# ============================================================
#  Partie 3/3 — Commandes Admin & Main
# ============================================================

# ============================================================
# DASHBOARD ADMIN
# ============================================================

async def handle_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    try:
        row_pending = await db_execute("SELECT COUNT(*) FROM pending_reports", mode="fetchone")
        row_muted = await db_execute("SELECT COUNT(*) FROM muted_users WHERE mute_until_ts > ?", (int(_now()),), "fetchone")
        row_edit = await db_execute("SELECT COUNT(*) FROM edit_state", mode="fetchone")
        pending_count, muted_count, edit_count = row_pending[0], row_muted[0], row_edit[0]
        member_count = await context.bot.get_chat_member_count(PUBLIC_GROUP_ID)
        uptime_sec = int(time.time() - START_TIME)
        d, h = divmod(uptime_sec // 3600, 24)
        m = (uptime_sec // 60) % 60
        uptime = f"{d}j {h}h {m}m"
        text = (
            f"📊 <b>Tableau de Bord - AccidentsFR Bot</b>\n"
            f"-----------------------------------\n"
            f"<b>État :</b> 🟢 En ligne\n"
            f"<b>Uptime :</b> {uptime}\n"
            f"<b>Signalements en attente :</b> {pending_count}\n"
            f"<b>Utilisateurs mutés :</b> {muted_count}\n"
            f"<b>Éditions en cours :</b> {edit_count}\n"
            f"<b>Membres publics :</b> {member_count}\n"
        )
        sent = await safe_send(msg.reply_text, text, parse_mode=ParseMode.HTML)
        asyncio.create_task(delete_after_delay([msg, sent], 60))
    except Exception as e:
        log.error(f"[DASHBOARD] {e}")
        await safe_send(msg.reply_text, f"Erreur dashboard : {e}")

# ============================================================
# LOCK / UNLOCK
# ============================================================

async def handle_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        return await safe_delete_message(context.bot, PUBLIC_GROUP_ID, msg.message_id)
    try:
        await context.bot.set_chat_permissions(PUBLIC_GROUP_ID, make_permissions(True))
        await db_execute("DELETE FROM bot_state WHERE key = 'lock_message_id'")
        sent = await safe_send(
            context.bot.send_message, PUBLIC_GROUP_ID,
            "🔒 Le chat a été verrouillé par un administrateur."
        )
        if sent:
            await db_execute("INSERT INTO bot_state (key, value) VALUES ('lock_message_id', ?)", (sent.message_id,))
        await safe_delete_message(context.bot, PUBLIC_GROUP_ID, msg.message_id)
    except Exception as e:
        log.error(f"[LOCK ERR] {e}")

async def handle_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        return await safe_delete_message(context.bot, PUBLIC_GROUP_ID, msg.message_id)
    try:
        await context.bot.set_chat_permissions(PUBLIC_GROUP_ID, make_permissions(False))
        row = await db_execute("SELECT value FROM bot_state WHERE key = 'lock_message_id'", mode="fetchone")
        if row:
            await safe_delete_message(context.bot, PUBLIC_GROUP_ID, int(row[0]))
            await db_execute("DELETE FROM bot_state WHERE key = 'lock_message_id'")
        sent = await safe_send(context.bot.send_message, PUBLIC_GROUP_ID, "🔓 Le chat est déverrouillé.")
        asyncio.create_task(delete_after_delay([sent], 5))
        await safe_delete_message(context.bot, PUBLIC_GROUP_ID, msg.message_id)
    except Exception as e:
        log.error(f"[UNLOCK ERR] {e}")

# ============================================================
# /DEPLACER (ADMIN)
# ============================================================

async def handle_deplacer_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg.reply_to_message:
        return await safe_send(msg.reply_text, "Usage : répondez à un message à publier.")
    original = msg.reply_to_message
    txt = (original.text or original.caption or "").strip().lower()
    topic = (
        PUBLIC_TOPIC_VIDEOS_ID if any(w in txt for w in accident_keywords)
        else PUBLIC_TOPIC_RADARS_ID if any(w in txt for w in radar_keywords)
        else PUBLIC_TOPIC_GENERAL_ID
    )
    try:
        if original.photo:
            await safe_send(context.bot.send_photo, PUBLIC_GROUP_ID, original.photo[-1].file_id, caption=original.caption, message_thread_id=topic)
        elif original.video:
            await safe_send(context.bot.send_video, PUBLIC_GROUP_ID, original.video.file_id, caption=original.caption, message_thread_id=topic)
        elif original.text:
            await safe_send(context.bot.send_message, PUBLIC_GROUP_ID, original.text, message_thread_id=topic)
        await safe_send(msg.reply_text, "✅ Publié dans le groupe public.")
        await safe_delete_message(context.bot, ADMIN_GROUP_ID, original.message_id)
    except Exception as e:
        log.error(f"[DEPLACER ADMIN] {e}")
        await safe_send(msg.reply_text, f"Erreur publication : {e}")

# ============================================================
# PUBLIC CLEANUP (COMMANDES NON-ADMINS)
# ============================================================

async def handle_public_admin_command_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        await safe_delete_message(context.bot, PUBLIC_GROUP_ID, msg.message_id)

# ============================================================
# POST INIT
# ============================================================

async def _post_init(application: Application):
    await init_db()
    asyncio.create_task(db_worker())
    asyncio.create_task(worker_loop(application))
    asyncio.create_task(cleaner_loop())
    log.info("✅ Post-init terminé. Base et workers démarrés.")

# ============================================================
# MAIN
# ============================================================

def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Handlers utilisateur
    app.add_handler(CommandHandler("start", handle_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    # Admin : modération / dashboard
    app.add_handler(CallbackQueryHandler(on_button_click))
    app.add_handler(CommandHandler("dashboard", handle_dashboard, filters=filters.Chat(ADMIN_GROUP_ID)))
    app.add_handler(CommandHandler("deplacer", handle_deplacer_admin, filters=filters.Chat(ADMIN_GROUP_ID) & filters.REPLY))

    # Public : admin controls
    app.add_handler(CommandHandler("lock", handle_lock, filters=filters.Chat(PUBLIC_GROUP_ID)))
    app.add_handler(CommandHandler("unlock", handle_unlock, filters=filters.Chat(PUBLIC_GROUP_ID)))
    app.add_handler(CommandHandler("dashboard", handle_public_admin_command_cleanup, filters=filters.Chat(PUBLIC_GROUP_ID)))
    app.add_handler(CommandHandler("cancel", handle_public_admin_command_cleanup, filters=filters.Chat(PUBLIC_GROUP_ID)))
    app.add_handler(CommandHandler("deplacer", handle_public_admin_command_cleanup, filters=filters.Chat(PUBLIC_GROUP_ID) & ~filters.REPLY))

    log.info("🚀 Bot AccidentsFR démarré — en écoute Telegram...")
    app.run_polling(poll_interval=POLL_INTERVAL, timeout=POLL_TIMEOUT)

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()