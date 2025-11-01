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
from telegram.error import Forbidden, BadRequest

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

PUBLIC_TOPIC_VIDEOS_ID = 224
PUBLIC_TOPIC_RADARS_ID = 222
PUBLIC_TOPIC_GENERAL_ID = None

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
REVIEW_QUEUE = asyncio.Queue()
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
            await db.commit()
        print(f"🗃️ DB ok '{DB_NAME}'")
    except Exception as e:
        print(f"[DB INIT ERR] {e}")
        raise

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

# --- Admin outbox : purge / track ---
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
        if len(text) >= 5:
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
                            can_send_stickers_and_emoji=False,
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

    # 5) Archivage médias (admin + public)
    if (chat_id in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID)) and (msg.photo or msg.video):
        if not is_spam:
            media_type = "video" if msg.video else "photo"
            file_id = msg.video.file_id if msg.video else msg.photo[-1].file_id
            caption = msg.caption or ""
            try:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        """
                        INSERT OR REPLACE INTO media_archive
                        (message_id, chat_id, media_group_id, file_id, file_type, caption, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (msg.message_id, chat_id, media_group_id, file_id, media_type, caption, int(now_ts))
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
        files_json = json.dumps(files_list)
        created_ts = int(_now())
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    "INSERT INTO pending_reports (report_id, text, files_json, created_ts, user_name) VALUES (?, ?, ?, ?, ?)",
                    (report_id, piece_text, files_json, created_ts, user_name)
                )
                await db.commit()
        except Exception as e:
            print(f"[DB INSERT] {e}")
            return
        await REVIEW_QUEUE.put({
            "report_id": report_id,
            "preview_text": _make_admin_preview(user_name, piece_text, is_album=False),
            "files": files_list,
        })
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
    if piece_text and not album["text"]:
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
    files_json = json.dumps(files_list)
    report_text = album["text"]
    created_ts = int(_now())
    user_name = album["user_name"]
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO pending_reports (report_id, text, files_json, created_ts, user_name) VALUES (?, ?, ?, ?, ?)",
                (report_id, report_text, files_json, created_ts, user_name)
            )
            await db.commit()
    except Exception as e:
        print(f"[DB INSERT ALBUM] {e}")
        return
    await REVIEW_QUEUE.put({
        "report_id": report_id,
        "preview_text": _make_admin_preview(user_name, report_text, is_album=True),
        "files": files_list,
    })
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
    try:
        m = await application.bot.send_message(
            chat_id=ADMIN_GROUP_ID, text=preview_text, reply_markup=kb,
        )
        sent_ids.append(m.message_id)

        if files:
            if len(files) == 1:
                f = files[0]
                if f["type"] == "photo":
                    pm = await application.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=f["file_id"])
                else:
                    pm = await application.bot.send_video(chat_id=ADMIN_GROUP_ID, video=f["file_id"])
                sent_ids.append(pm.message_id)
            else:
                media_group = []
                for f in files:
                    if f["type"] == "photo":
                        media_group.append(InputMediaPhoto(media=f["file_id"]))
                    else:
                        media_group.append(InputMediaVideo(media=f["file_id"]))
                msgs = await application.bot.send_media_group(chat_id=ADMIN_GROUP_ID, media=media_group)
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
        member_count = await context.bot.get_chat_member_count(PUBLIC_GROUP_ID)
        member_count = max(0, member_count - 2)

        uptime_seconds = int(time.time() - START_TIME)
        m, s = divmod(uptime_seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        uptime_str = f"{d}j {h}h {m}m"
        edit_status = "🟢 Non" if edit_count == 0 else f"🔴 OUI ({edit_count} verrou)"

        text = f"""
📊 <b>Tableau de Bord - AccidentsFR Bot</b>
-----------------------------------
<b>État :</b> 🟢 En ligne
<b>Disponibilité :</b> {uptime_str} (depuis {time.strftime('%d/%m %H:%M', time.localtime(START_TIME))})

<b>Modération :</b>
<b>Signalements en attente :</b> {pending_count}
<b>Utilisateurs mutés (privé) :</b> {muted_count}
<b>Édition en cours :</b> {edit_status}

<b>Activité :</b>
<b>Membres (Groupe Public) :</b> {member_count}

<i>(Ce message sera supprimé dans 60s)</i>
"""
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
            # Fallback
            if "Album non trouvé" in str(e) and (original_msg.photo or original_msg.video):
                print("[DEPLACER] Fallback déplacement simple")
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
# BOUTONS
# =========================
async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    action, report_id = data.split("|", 1)
    chat_id = query.message.chat_id
    
    # CORRIGÉ BUG: Connexion unique
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            # Clean éventuel edit_state concurrent
            try:
                await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
                await db.commit()
            except Exception as e:
                print(f"[BTN CLEAN EDIT_STATE] {e}")

            # 1. Lire les infos
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

            # --- REJECT ---
            if action == "REJECT":
                m = await context.bot.send_message(ADMIN_GROUP_ID, "❌ Supprimé, non publié.")
                asyncio.create_task(delete_after_delay([m], 5))
                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()
                await admin_outbox_delete(report_id, context.bot)
                return

            # --- REJECT & MUTE ---
            if action == "REJECTMUTE":
                user_id = None
                try:
                    user_id_str, _ = report_id.split("_", 1)
                    user_id = int(user_id_str)
                except Exception:
                    pass
                
                mute_duration = MUTE_DURATION_SPAM_SUBMISSION
                mute_until_ts = int(_now() + mute_duration)
                
                await db.execute(
                    "INSERT OR REPLACE INTO muted_users (user_id, mute_until_ts) VALUES (?, ?)",
                    (user_id, mute_until_ts)
                )
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

            # --- EDIT (prompt) ---
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

            # --- APPROVE ---
            if action == "APPROVE":
                files = info["files"]
                text = (info["text"] or "").strip()
                caption_for_public = text if text else None
                text_lower = text.lower() if text else ""
                
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
                    
                    try:
                        user_chat_id_str, _ = report_id.split("_", 1)
                        user_chat_id = int(user_chat_id_str)
                        await context.bot.send_message(
                            chat_id=user_chat_id,
                            text="✅ Ton signalement a été publié dans le canal @AccidentsFR."
                        )
                    except Exception as e:
                        print(f"[NOTIFY USER APPROVE] {e}")

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
    while True:
        try:
            item = await REVIEW_QUEUE.get()
            rid = item["report_id"]
            preview = item["preview_text"]
            files = item["files"]
            await send_report_to_admin(application, rid, preview, files)
            REVIEW_QUEUE.task_done()
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
                if int(now) % 3600 == 0:
                    await db.execute("DELETE FROM edit_state")
                    await db.execute("DELETE FROM muted_users WHERE mute_until_ts < ?", (int(now),))
                    await db.execute("DELETE FROM admin_outbox") # Purge de sécurité
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
# COMMANDES /lock et /unlock
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
    can_send_stickers_and_emoji=True, # CORRIGÉ
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
    can_send_stickers_and_emoji=False, # CORRIGÉ
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
        await context.bot.set_chat_permissions(
            chat_id=PUBLIC_GROUP_ID,
            permissions=LOCK_PERMISSIONS
        )
        
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
        await context.bot.set_chat_permissions(
            chat_id=PUBLIC_GROUP_ID,
            permissions=DEFAULT_PERMISSIONS
        )
        
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

# NOUVEAU : Handler pour nettoyer les commandes admin tapées dans le public
async def handle_public_admin_command_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Supprime les commandes admin (/dashboard, /cancel) si tapées par un non-admin dans le public."""
    msg = update.message
    if not await is_user_admin(context, PUBLIC_GROUP_ID, msg.from_user.id):
        try:
            await msg.delete()
        except Exception:
            pass

# NOUVEAU : Commande /start (accueil en privé)
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    # Message d'accueil (avec formatage HTML pour le gras)
    welcome_text = """
Bonjour ! Je suis le bot officiel de <b>@AccidentsFR</b>.

🤫 <b>Toutes vos soumissions ici sont 100% ANONYMES.</b>

---

<b>Comment ça marche ?</b>

1.  Envoyez-moi simplement vos photos, vidéos, ou infos (radars, accidents, contrôles).

2.  N'oubliez pas d'ajouter un petit texte pour le <b>contexte</b> (ex: "Radar mobile A7, sortie Montélimar" ou "Dashcam accident N104").

3.  Un admin validera votre signalement.

4.  Il sera ensuite <b>publié instantanément</b> dans le bon topic du groupe @AccidentsFR (📍 Radars ou 🎥 Vidéos).

---
À vous de jouer !
"""
    
    try:
        await msg.reply_text(welcome_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[HANDLE START] Erreur: {e}")


# =========================
# MAIN
# =========================
async def _post_init(application: Application):
    try:
        await init_db()
        asyncio.create_task(worker_loop(application))
        asyncio.create_task(cleaner_loop())
    except Exception as e:
        print(f"[POST_INIT] {e}")

def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .post_init(_post_init)
           .build())

    # --- HANDLERS ---
    
    # NOUVEAU : Commande /start (en privé)
    app.add_handler(CommandHandler("start", handle_start, filters=filters.ChatType.PRIVATE))

    # Commandes Admin (Groupe Admin)
    app.add_handler(CommandHandler("cancel", handle_admin_cancel, filters=filters.Chat(ADMIN_GROUP_ID)))
    app.add_handler(CommandHandler("dashboard", handle_dashboard, filters=filters.Chat(ADMIN_GROUP_ID)))
    app.add_handler(CommandHandler("deplacer", handle_deplacer_admin, filters=filters.Chat(ADMIN_GROUP_ID) & filters.REPLY))
    
    # Commandes Admin (Groupe Public)
    app.add_handler(CommandHandler("lock", handle_lock, filters=filters.Chat(PUBLIC_GROUP_ID)))
    app.add_handler(CommandHandler("unlock", handle_unlock, filters=filters.Chat(PUBLIC_GROUP_ID)))
    app.add_handler(CommandHandler(
        "deplacer",
        handle_deplacer_public,
        filters=filters.Chat(PUBLIC_GROUP_ID) & filters.REPLY
    ))
    
    # Nettoyage commandes admin (Groupe Public)
    app.add_handler(CommandHandler(
        ["dashboard", "cancel"], 
        handle_public_admin_command_cleanup, 
        filters=filters.Chat(PUBLIC_GROUP_ID)
    ))
    app.add_handler(CommandHandler(
        "deplacer", 
        handle_public_admin_command_cleanup, 
        filters=filters.Chat(PUBLIC_GROUP_ID) & ~filters.REPLY
    ))
    
    # Callbacks (Boutons)
    app.add_handler(CallbackQueryHandler(on_button_click))
    
    # Messages texte (admin)
    app.add_handler(MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND, handle_admin_edit))
    
    # Handler "attrape-tout"
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    
    # --- FIN DES HANDLERS ---

    print("🚀 Bot démarré, en écoute…")
    app.run_polling(poll_interval=POLL_INTERVAL, timeout=POLL_TIMEOUT)

if __name__ == "__main__":
    main()
