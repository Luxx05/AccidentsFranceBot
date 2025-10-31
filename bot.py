import os
import time
import threading
import asyncio
import json
import aiosqlite
import requests
from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    CommandHandler,
)
from telegram.error import Forbidden, BadRequest

# =========================
# Meta
# =========================
START_TIME = time.time()

# =========================
# CONFIG
# =========================
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
CLEAN_MAX_AGE_ARCHIVE = 3600 * 24 * 3

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
# Mémoire volatile
# =========================
LAST_MSG_TIME = {}
SPAM_COUNT = {}
TEMP_ALBUMS = {}
REVIEW_QUEUE = asyncio.Queue()
ALREADY_FORWARDED_ALBUMS = set()

# =========================
# DB INIT
# =========================
async def init_db():
    print("🗃️ Initialisation de la base de données SQLite...")
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_reports (
                    report_id TEXT PRIMARY KEY,
                    text TEXT,
                    files_json TEXT,
                    created_ts INTEGER,
                    user_name TEXT,
                    admin_media_msg_ids TEXT
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
            # En cas d’ancienne BDD, tente d’ajouter la colonne (ignore si déjà là)
            try:
                await db.execute("ALTER TABLE pending_reports ADD COLUMN admin_media_msg_ids TEXT")
            except Exception:
                pass

            await db.commit()
        print(f"🗃️ Base de données prête sur '{DB_NAME}'.")
    except Exception as e:
        print(f"[ERREUR DB INIT] {e}")
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
            print(f"[DELETE_AFTER_DELAY] Erreur: {e}")

async def _clean_admin_media_ids(context: ContextTypes.DEFAULT_TYPE, report_id: str):
    """Supprime les médias d'aperçu admin liés à report_id."""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as c:
                await c.execute("SELECT admin_media_msg_ids FROM pending_reports WHERE report_id = ?", (report_id,))
                r = await c.fetchone()
        if not r or not r[0]:
            return
        ids = []
        try:
            ids = json.loads(r[0])
        except Exception:
            ids = []
        for mid in ids:
            try:
                await context.bot.delete_message(ADMIN_GROUP_ID, mid)
            except Exception as e:
                print(f"[CLEAN ADMIN MEDIA] del {mid} fail: {e}")
    except Exception as e:
        print(f"[CLEAN ADMIN MEDIA] read fail: {e}")

# =========================
# GESTION CONTENU UTILISATEUR
# =========================
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # 1) Nettoyage messages de service
    if (
        msg.new_chat_members or
        msg.left_chat_member or
        msg.new_chat_photo or
        msg.delete_chat_photo or
        msg.new_chat_title
    ):
        if msg.chat_id in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID):
            try:
                await msg.delete()
                return
            except Exception:
                pass
        return

    # 2) Général
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
            print(f"[ERREUR CHECK MUTE] {e}")

    # 4) Anti-spam public
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
                            can_send_media_messages=False,
                            can_send_other_messages=False,
                            can_add_web_page_previews=False
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

    # 5) Archivage médias (Admin + Public)
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
                print(f"[ARCHIVAGE DB] Erreur: {e}")
        return

    # 6) Ignorer texte non-commande dans groupes
    if chat_id in (PUBLIC_GROUP_ID, ADMIN_GROUP_ID):
        return

    # 7) Traitement PV (soumissions)
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

    # Pas album
    if media_group_id is None:
        report_id = f"{chat_id}_{msg.id}"
        files_list = []
        if media_type and file_id:
            files_list.append({"type": media_type, "file_id": file_id})
        files_json = json.dumps(files_list)
        created_ts = int(_now())
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    "INSERT INTO pending_reports (report_id, text, files_json, created_ts, user_name, admin_media_msg_ids) VALUES (?, ?, ?, ?, ?, ?)",
                    (report_id, piece_text, files_json, created_ts, user_name, json.dumps([]))
                )
                await db.commit()
        except Exception as e:
            print(f"[ERREUR DB INSERT] {e}")
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

    # Album
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
                "INSERT INTO pending_reports (report_id, text, files_json, created_ts, user_name, admin_media_msg_ids) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, report_text, files_json, created_ts, user_name, json.dumps([]))
            )
            await db.commit()
    except Exception as e:
        print(f"[ERREUR DB INSERT ALBUM] {e}")
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
# GESTION ADMIN
# =========================
async def send_report_to_admin(application: Application, report_id: str, preview_text: str, files: list[dict]):
    """Envoie l’aperçu au chat admin + stocke les message_id des médias pour cleanup."""
    kb = _build_mod_keyboard(report_id)

    # 1) Preview texte avec boutons
    try:
        await application.bot.send_message(
            chat_id=ADMIN_GROUP_ID, text=preview_text, reply_markup=kb,
        )
    except Exception as e:
        print(f"[ADMIN SEND] erreur (texte) : {e}")
        return

    # 2) Médias
    media_ids = []
    if files:
        try:
            if len(files) == 1:
                m = files[0]
                if m["type"] == "photo":
                    sent = await application.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=m["file_id"])
                else:
                    sent = await application.bot.send_video(chat_id=ADMIN_GROUP_ID, video=m["file_id"])
                media_ids = [sent.message_id]
            else:
                media_group = []
                for m in files:
                    if m["type"] == "photo":
                        media_group.append(InputMediaPhoto(media=m["file_id"]))
                    else:
                        media_group.append(InputMediaVideo(media=m["file_id"]))
                sent_group = await application.bot.send_media_group(chat_id=ADMIN_GROUP_ID, media=media_group)
                media_ids = [m.message_id for m in sent_group]
        except Exception as e:
            print(f"[ADMIN SEND] erreur médias : {e}")

    # 3) Sauvegarder les IDs de ces médias pour pouvoir les supprimer plus tard
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE pending_reports SET admin_media_msg_ids = ? WHERE report_id = ?",
                (json.dumps(media_ids), report_id)
            )
            await db.commit()
    except Exception as e:
        print(f"[ADMIN SEND] save media ids fail: {e}")

async def handle_admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat_id
    report_id = None
    prompt_message_id = None

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as cursor:
                await cursor.execute("SELECT report_id, prompt_message_id FROM edit_state WHERE chat_id = ?", (chat_id,))
                row = await cursor.fetchone()
                if row:
                    report_id, prompt_message_id = row
            if not report_id:
                return

            # 1) Finir l’édition: on supprime l’état et on remplace le texte
            await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
            new_text = msg.text
            await db.execute("UPDATE pending_reports SET text = ? WHERE report_id = ?", (new_text, report_id))
            await db.commit()

            # 2) Charger le report complet
            async with db.cursor() as cursor:
                await cursor.execute(
                    "SELECT text, files_json, user_name, admin_media_msg_ids FROM pending_reports WHERE report_id = ?",
                    (report_id,)
                )
                row = await cursor.fetchone()
            if not row:
                await msg.reply_text("Erreur : Signalement introuvable après mise à jour.")
                return

            text, files_json, user_name, _ = row
            files = json.loads(files_json)

        # 3) Nettoyage des anciens médias d’aperçu admin
        await _clean_admin_media_ids(context, report_id)

        # 4) Confirma + renvoi du NOUVEL aperçu (et mise à jour des media ids)
        sent_confirmation_msg = await msg.reply_text("✅ Texte mis à jour. Aperçu actualisé…")
        preview_text = _make_admin_preview(user_name, text, is_album=len(files) > 1)
        await send_report_to_admin(context.application, report_id, preview_text, files)

        # 5) Nettoyage des messages d’édition
        await msg.delete()
        await sent_confirmation_msg.delete()
        if prompt_message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
            except Exception:
                pass

    except Exception as e:
        print(f"[HANDLE ADMIN EDIT - DB] {e}")
        try:
            await msg.reply_text(f"Une erreur est survenue lors de la mise à jour : {e}")
        except Exception:
            pass

async def handle_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    prompt_message_id = None
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as cursor:
                await cursor.execute("SELECT prompt_message_id FROM edit_state WHERE chat_id = ?", (chat_id,))
                row = await cursor.fetchone()

                if row:
                    prompt_message_id = row[0]
                    await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
                    await db.commit()

                    sent_msg = await msg.reply_text("Modification annulée.")

                    await msg.delete()
                    asyncio.create_task(delete_after_delay([sent_msg], 5))
                    if prompt_message_id:
                        try:
                            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
                        except Exception:
                            pass
                else:
                    sent_msg = await msg.reply_text("Vous n'étiez pas en train de modifier un message.")
                    asyncio.create_task(delete_after_delay([msg, sent_msg], 5))
    except Exception as e:
        print(f"[HANDLE ADMIN CANCEL] {e}")

async def handle_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    try:
        pending_count = 0
        muted_count = 0
        edit_count = 0

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as cursor:
                await cursor.execute("SELECT COUNT(*) FROM pending_reports")
                pending_count = (await cursor.fetchone())[0]
                await cursor.execute("SELECT COUNT(*) FROM muted_users WHERE mute_until_ts > ?", (int(_now()),))
                muted_count = (await cursor.fetchone())[0]
                await cursor.execute("SELECT COUNT(*) FROM edit_state")
                edit_count = (await cursor.fetchone())[0]

        member_count = await context.bot.get_chat_member_count(PUBLIC_GROUP_ID)
        member_count = max(0, member_count - 2)

        uptime_seconds = int(time.time() - START_TIME)
        m, s = divmod(uptime_seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        uptime_str = f"{d}j {h}h {m}m"
        edit_status = "🟢 Non" if edit_count == 0 else f"🔴 OUI ({edit_count} verrou)"

    except Exception as e:
        print(f"[DASHBOARD] Erreur BDD/API: {e}")
        try:
            sent_msg = await msg.reply_text(f"Erreur lors de la récupération des stats : {e}")
            asyncio.create_task(delete_after_delay([msg, sent_msg], 60))
        except Exception:
            pass
        return

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
    try:
        sent_msg = await msg.reply_text(text, parse_mode=ParseMode.HTML)
        asyncio.create_task(delete_after_delay([msg, sent_msg], 60))
    except Exception as e:
        print(f"[DASHBOARD] Erreur envoi: {e}")

async def handle_deplacer_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    original_msg = msg.reply_to_message
    if not original_msg:
        try:
            await msg.reply_text("Usage: répondez à votre message avec /deplacer pour le publier.")
        except Exception:
            pass
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
            album_items = []
            album_caption = ""
            message_ids_to_delete = []
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.cursor() as cursor:
                    await cursor.execute(
                        "SELECT message_id, file_type, file_id, caption FROM media_archive WHERE media_group_id = ? AND chat_id = ? ORDER BY message_id",
                        (media_group_id, ADMIN_GROUP_ID)
                    )
                    rows = await cursor.fetchall()
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
                chat_id=PUBLIC_GROUP_ID,
                media=album_items,
                message_thread_id=target_thread_id
            )
            for msg_id in message_ids_to_delete:
                try:
                    await context.bot.delete_message(ADMIN_GROUP_ID, msg_id)
                except Exception as e:
                    print(f"[DEPLACER_ADMIN] Erreur suppression msg album {msg_id}: {e}")
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
                await msg.reply_text("Ce type de message (ex: sticker) ne peut pas être publié.")
                return
            await original_msg.delete()

        m = await msg.reply_text("✅ Message publié dans le groupe public.")
        asyncio.create_task(delete_after_delay([msg, m], 5))

    except Exception as e:
        print(f"[DEPLACER_ADMIN] Erreur publication : {e}")
        try:
            await msg.reply_text(f"Erreur lors de la publication : {e}")
        except Exception:
            pass

async def handle_deplacer_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    try:
        user_id = msg.from_user.id
        is_admin_check_passed = False
        if user_id in [1087968824, 136817688]:
            is_admin_check_passed = True
        else:
            admins_list = await context.bot.get_chat_administrators(PUBLIC_GROUP_ID)
            admin_ids = {admin.user.id for admin in admins_list}
            if user_id in admin_ids:
                is_admin_check_passed = True
        if not is_admin_check_passed:
            return
    except Exception as e:
        print(f"[DEPLACER] Erreur vérification admin : {e}")
        return

    original_msg = msg.reply_to_message
    if not original_msg:
        try:
            await msg.reply_text("Usage: répondez à un message avec /deplacer")
        except Exception:
            pass
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
            m = await msg.reply_text("Ce message est déjà dans le bon topic.")
            asyncio.create_task(delete_after_delay([msg, m], 3))
        except Exception:
            pass
        return

    try:
        if media_group_id:
            album_items = []
            album_caption = ""
            message_ids_to_delete = []
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.cursor() as cursor:
                    await cursor.execute(
                        "SELECT message_id, file_type, file_id, caption FROM media_archive WHERE media_group_id = ? AND chat_id = ? ORDER BY message_id",
                        (media_group_id, PUBLIC_GROUP_ID)
                    )
                    rows = await cursor.fetchall()
            if not rows:
                raise Exception("Album non trouvé dans l'archive, déplacement simple.")
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
                chat_id=PUBLIC_GROUP_ID,
                media=album_items,
                message_thread_id=target_thread_id
            )
            for msg_id in message_ids_to_delete:
                try:
                    await context.bot.delete_message(PUBLIC_GROUP_ID, msg_id)
                except Exception as e:
                    print(f"[DEPLACER] Erreur suppression msg album {msg_id}: {e}")
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
                await msg.reply_text("Ce type de message (ex: sticker) ne peut pas être déplacé.")
                return
            await original_msg.delete()
        await msg.delete()
    except Exception as e:
        print(f"[DEPLACER] Erreur publication/suppression : {e}")
        try:
            if "Album non trouvé" in str(e) and (original_msg.photo or original_msg.video):
                print("[DEPLACER] Fallback en déplacement simple")
                photo = original_msg.photo[-1].file_id if original_msg.photo else None
                video = original_msg.video.file_id if original_msg.video else None
                if photo:
                    await context.bot.send_photo(
                        chat_id=PUBLIC_GROUP_ID, photo=photo, caption=text_to_analyze, message_thread_id=target_thread_id
                    )
                elif video:
                    await context.bot.send_video(
                        chat_id=PUBLIC_GROUP_ID, video=video, caption=text_to_analyze, message_thread_id=target_thread_id
                    )
                await original_msg.delete()
                await msg.delete()
            else:
                await msg.reply_text(f"Erreur lors du déplacement : {e}")
        except Exception:
            pass

async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    action, report_id = data.split("|", 1)
    info = None
    sent_msg = None
    chat_id = query.message.chat_id

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            # Toujours nettoyer un éventuel état d'édition actif pour ce chat
            try:
                await db.execute("DELETE FROM edit_state WHERE chat_id = ?", (chat_id,))
                await db.commit()
            except Exception as e:
                print(f"[ON BUTTON CLICK - DB CLEAN] {e}")

            async with db.cursor() as cursor:
                await cursor.execute("SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?", (report_id,))
                row = await cursor.fetchone()
                if row:
                    text_from_db, files_json_from_db, user_name_from_db = row
                    info = {
                        "text": text_from_db,
                        "files": json.loads(files_json_from_db),
                        "user_name": user_name_from_db
                    }
        if not info:
            try:
                sent_msg = await query.edit_message_text("🚫 Déjà traité / introuvable.")
                asyncio.create_task(delete_after_delay([sent_msg], 5))
            except Exception:
                pass
            return

        # REJECT
        if action == "REJECT":
            await _clean_admin_media_ids(context, report_id)  # 🔥 cleanup anciens médias du preview
            try:
                sent_msg = await query.edit_message_text("❌ Supprimé, non publié.")
                asyncio.create_task(delete_after_delay([sent_msg], 5))
            except Exception:
                pass
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()
            return

        # REJECT & MUTE
        if action == "REJECTMUTE":
            user_id = None
            try:
                user_id_str, _ = report_id.split("_", 1)
                user_id = int(user_id_str)
                mute_duration = MUTE_DURATION_SPAM_SUBMISSION
                mute_until_ts = int(_now() + mute_duration)
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO muted_users (user_id, mute_until_ts) VALUES (?, ?)",
                        (user_id, mute_until_ts)
                    )
                    await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                    await db.commit()
                await _clean_admin_media_ids(context, report_id)  # 🔥 cleanup preview
                sent_msg = await query.edit_message_text("🔇 Rejeté. Utilisateur muté pour 1 heure.")
                asyncio.create_task(delete_after_delay([sent_msg], 5))
                if user_id:
                    mute_hours = mute_duration // 3600
                    message_text = f"❌ Votre soumission a été rejetée.\n\nVous avez été restreint d'envoyer de nouveaux signalements pour {mute_hours} heure(s) pour cause de spam/abus."
                    await context.bot.send_message(chat_id=user_id, text=message_text)
            except Exception as e:
                print(f"[ERREUR REJECTMUTE] {e}")
                try:
                    sent_msg = await query.edit_message_text(f"Erreur lors du mute: {e}")
                    asyncio.create_task(delete_after_delay([sent_msg], 10))
                except Exception:
                    pass
            return

        # EDIT
        if action == "EDIT":
            current_text = info.get("text", "")
            try:
                sent_prompt_msg = await query.edit_message_text(
                    f"✏️ **Modification en cours...**\n\n**Texte actuel :**\n`{current_text}`\n\nEnvoyez le nouveau texte. (ou envoyez /cancel pour annuler)",
                    reply_markup=None, parse_mode="Markdown"
                )
                prompt_message_id = sent_prompt_msg.message_id
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO edit_state (chat_id, report_id, prompt_message_id) VALUES (?, ?, ?)",
                        (chat_id, report_id, prompt_message_id)
                    )
                    await db.commit()
            except Exception as e:
                print(f"[EDIT BUTTON] Erreur (ou édition déjà en cours): {e}")
                try:
                    await context.bot.delete_message(chat_id, prompt_message_id)
                    sent_msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Une modification est déjà en cours. Veuillez terminer ou annuler (/cancel) la précédente."
                    )
                    asyncio.create_task(delete_after_delay([sent_msg], 10))
                except Exception:
                    pass
            return

        # APPROVE
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
                        sent_msg = await query.edit_message_text("✅ Publié (texte).")
                    else:
                        sent_msg = await query.edit_message_text("❌ Rien à publier (vide).")
                elif len(files) == 1:
                    m = files[0]
                    if m["type"] == "photo":
                        await context.bot.send_photo(
                            chat_id=PUBLIC_GROUP_ID, photo=m["file_id"],
                            caption=caption_for_public, message_thread_id=target_thread_id
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=PUBLIC_GROUP_ID, video=m["file_id"],
                            caption=caption_for_public, message_thread_id=target_thread_id
                        )
                    sent_msg = await query.edit_message_text("✅ Publié dans le groupe public.")
                else:
                    media_group = []
                    for i, m in enumerate(files):
                        caption = caption_for_public if i == 0 else None
                        if m["type"] == "photo":
                            media_group.append(InputMediaPhoto(media=m["file_id"], caption=caption))
                        else:
                            media_group.append(InputMediaVideo(media=m["file_id"], caption=caption))
                    await context.bot.send_media_group(
                        chat_id=PUBLIC_GROUP_ID, media=media_group,
                        message_thread_id=target_thread_id
                    )
                    sent_msg = await query.edit_message_text("✅ Publié (album) dans le groupe public.")

                if sent_msg:
                    asyncio.create_task(delete_after_delay([sent_msg], 5))

            except Exception as e:
                print(f"[ERREUR PUBLICATION] {e}")
                try:
                    sent_msg = await query.edit_message_text(f"⚠️ Erreur publication: {e}")
                    asyncio.create_task(delete_after_delay([sent_msg], 10))
                except Exception:
                    pass
                return

            # Notifier l'utilisateur en PV (best effort)
            try:
                user_chat_id_str, _ = report_id.split("_", 1)
                user_chat_id = int(user_chat_id_str)
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text="✅ Ton signalement a été publié dans le canal @AccidentsFR."
                )
            except Exception as e:
                print(f"[ERREUR NOTIFY USER] {e}")

            # 🔥 cleanup preview admin (médias)
            await _clean_admin_media_ids(context, report_id)

            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()
            return

    except Exception as e:
        print(f"[ERREUR ON_BUTTON_CLICK - GLOBAL] {e}")

# =========================
# WORKERS
# =========================
async def worker_loop(application: Application):
    print("👷 Worker (asyncio) démarré")
    while True:
        try:
            item = await REVIEW_QUEUE.get()
            rid = item["report_id"]
            preview = item["preview_text"]
            files = item["files"]
            await send_report_to_admin(application, rid, preview, files)
            REVIEW_QUEUE.task_done()
        except Exception as e:
            print(f"[ERREUR WORKER] {e}")
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
            print(f"[ERREUR CLEANER] {e}")

# =========================
# KEEP-ALIVE Render Free
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
# MAIN
# =========================
async def _post_init(application: Application):
    try:
        await init_db()
        asyncio.create_task(worker_loop(application))
        asyncio.create_task(cleaner_loop())
    except Exception as e:
        print(f"[ERREUR POST_INIT] {e}")

def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .post_init(_post_init)
           .build())

    # Handlers
    app.add_handler(CallbackQueryHandler(on_button_click))

    app.add_handler(CommandHandler(
        "cancel",
        handle_admin_cancel,
        filters=filters.Chat(ADMIN_GROUP_ID)
    ))

    app.add_handler(CommandHandler(
        "dashboard",
        handle_dashboard,
        filters=filters.Chat(ADMIN_GROUP_ID)
    ))

    app.add_handler(CommandHandler(
        "deplacer",
        handle_deplacer_admin,
        filters=filters.Chat(ADMIN_GROUP_ID) & filters.REPLY
    ))

    app.add_handler(CommandHandler(
        "deplacer",
        handle_deplacer_public,
        filters=filters.Chat(PUBLIC_GROUP_ID) & filters.REPLY
    ))

    app.add_handler(MessageHandler(
        filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
        handle_admin_edit
    ))

    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_user_message
    ))

    print("🚀 Bot démarré, en écoute…")
    app.run_polling(
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )

if __name__ == "__main__":
    main()