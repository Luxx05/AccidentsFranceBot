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
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    CommandHandler,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1003294631521"))
PUBLIC_GROUP_ID = int(os.getenv("PUBLIC_GROUP_ID", "-1003245719893"))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "https://accidentsfrancebot.onrender.com")

DB_NAME = os.getenv("DB_PATH", "bot_storage.db") 

SPAM_COOLDOWN = 4
MUTE_THRESHOLD = 3
MUTE_DURATION_SEC = 300

CLEAN_MAX_AGE_PENDING = 3600 * 24
CLEAN_MAX_AGE_ALBUMS = 60
CLEAN_MAX_AGE_SPAM = 3600

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 30

# --- Topics du groupe public ---
PUBLIC_TOPIC_VIDEOS_ID = 224
PUBLIC_TOPIC_RADARS_ID = 222
# (Laissez PUBLIC_TOPIC_GENERAL_ID à 'None' si vous utilisez le topic "Général" par défaut)
PUBLIC_TOPIC_GENERAL_ID = None 

# --- Mots-clés pour le tri ---
# (Déplacés ici pour être utilisés par /deplacer ET on_button_click)
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


# =========================================================
# STOCKAGE EN MÉMOIRE (Volatile)
# =========================================================

LAST_MSG_TIME = {}
SPAM_COUNT = {}
TEMP_ALBUMS = {}
REVIEW_QUEUE = asyncio.Queue()
ALREADY_FORWARDED_ALBUMS = set()

# =========================================================
# INITIALISATION BASE DE DONNÉES
# =========================================================

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
                    user_name TEXT 
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS edit_state (
                    admin_id INTEGER PRIMARY KEY,
                    report_id TEXT
                )
            """)
            await db.commit()
        print(f"🗃️ Base de données prête sur '{DB_NAME}'.")
    except Exception as e:
        print(f"[ERREUR DB INIT] {e}")
        raise

# =========================================================
# OUTILS
# =========================================================

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
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
        ]
    ])

# =========================================================
# GESTION DU CONTENU UTILISATEUR
# =========================================================

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # 1. NETTOYAGE DES MESSAGES DE SERVICE (join/left/photo)
    if (
        msg.new_chat_members or
        msg.left_chat_member or
        msg.new_chat_photo or
        msg.delete_chat_photo or
        msg.new_chat_title
    ):
        if msg.chat_id == PUBLIC_GROUP_ID or msg.chat_id == ADMIN_GROUP_ID:
            try:
                await msg.delete()
                print(f"[CLEANER] Service message deleted in {msg.chat_id}")
                return
            except Exception as e:
                print(f"[CLEANER] Failed to delete service message: {e}")
        return

    # 2. GESTION DES MESSAGES NORMAUX
    user = msg.from_user
    chat_id = msg.chat_id
    media_group_id = msg.media_group_id
    
    # 3. ANTI-SPAM (GROUPE PUBLIC)
    if chat_id == PUBLIC_GROUP_ID:
        text_raw = (msg.text or msg.caption or "").strip()
        text = text_raw.lower()
        now_ts = _now()
        user_state = SPAM_COUNT.get(user.id, {"count": 0, "last": 0})
        flood = _is_spam(user.id, media_group_id)
        gibberish = False
        if len(text) >= 5:
            consonnes = sum(1 for c in text if c in "bcdfghjklmnpqrstvwxyz")
            voyelles = sum(1 for c in text if c in "aeiouy")
            ratio = consonnes / (voyelles + 1)
            if ratio > 5:
                gibberish = True
        if flood or gibberish:
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
                        chat_id=PUBLIC_GROUP_ID, user_id=user.id,
                        permissions={"can_send_messages": False, "can_send_media_messages": False, "can_send_other_messages": False, "can_add_web_page_previews": False},
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
    # === fin anti-spam ===

    # 4. IGNORER LES MESSAGES (non-commandes) DES GROUPES
    if chat_id == PUBLIC_GROUP_ID or chat_id == ADMIN_GROUP_ID:
        return

    # 5. TRAITEMENT DES MESSAGES PRIVÉS (SOUMISSIONS)
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

    # ===== CAS 1 : PAS ALBUM =====
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
                    "INSERT INTO pending_reports (report_id, text, files_json, created_ts, user_name) VALUES (?, ?, ?, ?, ?)",
                    (report_id, piece_text, files_json, created_ts, user_name)
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

    # ===== CAS 2 : ALBUM =====
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

# =========================================================
# GESTION ADMIN
# =========================================================

async def send_report_to_admin(application, report_id: str, preview_text: str, files: list[dict]):
    kb = _build_mod_keyboard(report_id)
    try:
        await application.bot.send_message(
            chat_id=ADMIN_GROUP_ID, text=preview_text, reply_markup=kb,
        )
    except Exception as e:
        print(f"[ADMIN SEND] erreur (texte) : {e}")
        return
    if not files:
        return
    if len(files) == 1:
        m = files[0]
        try:
            if m["type"] == "photo":
                await application.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=m["file_id"])
            else:
                await application.bot.send_video(chat_id=ADMIN_GROUP_ID, video=m["file_id"])
        except Exception as e:
            print(f"[ADMIN SEND] erreur single media : {e}")
        return
    media_group = []
    for m in files:
        if m["type"] == "photo":
            media_group.append(InputMediaPhoto(media=m["file_id"]))
        else:
            media_group.append(InputMediaVideo(media=m["file_id"]))
    try:
        await application.bot.send_media_group(chat_id=ADMIN_GROUP_ID, media=media_group)
    except Exception as e:
        print(f"[ADMIN SEND] erreur album media_group : {e}")


async def handle_admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    admin_user_id = msg.from_user.id
    report_id = None
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as cursor:
                await cursor.execute("SELECT report_id FROM edit_state WHERE admin_id = ?", (admin_user_id,))
                row = await cursor.fetchone()
                if row:
                    report_id = row[0]
            if not report_id:
                return
            await db.execute("DELETE FROM edit_state WHERE admin_id = ?", (admin_user_id,))
            new_text = msg.text
            await db.execute("UPDATE pending_reports SET text = ? WHERE report_id = ?", (new_text, report_id))
            await db.commit()
            async with db.cursor() as cursor:
                await cursor.execute("SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?", (report_id,))
                row = await cursor.fetchone()
            if not row:
                await msg.reply_text("Erreur : Signalement introuvable après mise à jour.")
                return
            text, files_json, user_name = row
            files = json.loads(files_json)
            await msg.reply_text(f"✅ Texte mis à jour. Voici le nouvel aperçu :")
            preview_text = _make_admin_preview(user_name, text, is_album=len(files) > 1)
            await send_report_to_admin(context.application, report_id, preview_text, files)
    except Exception as e:
        print(f"[HANDLE ADMIN EDIT - DB] {e}")
        await msg.reply_text(f"Une erreur est survenue lors de la mise à jour : {e}")


async def handle_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = update.message.from_user.id
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM edit_state WHERE admin_id = ?", (admin_user_id,))
                row = await cursor.fetchone()
                if row:
                    await db.execute("DELETE FROM edit_state WHERE admin_id = ?", (admin_user_id,))
                    await db.commit()
                    await update.message.reply_text("Modification annulée. Vous pouvez à nouveau utiliser les boutons.")
                else:
                    await update.message.reply_text("Vous n'étiez pas en train de modifier un message.")
    except Exception as e:
        print(f"[HANDLE ADMIN CANCEL] {e}")


# NOUVELLE FONCTIONNALITÉ : Commande /deplacer
async def handle_deplacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    # 1. Vérifier si c'est un admin
    try:
        admins_list = await context.bot.get_chat_administrators(PUBLIC_GROUP_ID)
        admin_ids = {admin.user.id for admin in admins_list}
        if msg.from_user.id not in admin_ids:
            print(f"[DEPLACER] Ignoré (non-admin) : {msg.from_user.id}")
            return # Ignore silencieusement si ce n'est pas un admin
    except Exception as e:
        print(f"[DEPLACER] Erreur vérification admin : {e}")
        return

    # 2. Vérifier si c'est une réponse
    original_msg = msg.reply_to_message
    if not original_msg:
        try:
            await msg.reply_text("Usage: répondez à un message avec /deplacer")
        except Exception: pass
        return

    # 3. Analyser le contenu du message original
    text_to_analyze = (original_msg.text or original_msg.caption or "").strip()
    text_lower = text_to_analyze.lower()
    
    photo = original_msg.photo[-1].file_id if original_msg.photo else None
    video = original_msg.video.file_id if original_msg.video else None

    # 4. Déterminer le topic de destination
    target_thread_id = None
    if any(word in text_lower for word in accident_keywords):
        target_thread_id = PUBLIC_TOPIC_VIDEOS_ID
    elif any(word in text_lower for word in radar_keywords):
        target_thread_id = PUBLIC_TOPIC_RADARS_ID
    else:
        # Si aucun mot-clé, on peut choisir de le mettre dans Général
        # ou (mieux) de ne rien faire.
        target_thread_id = PUBLIC_TOPIC_GENERAL_ID # 'None' déplace vers "Général"

    # 5. Vérifier si le message est déjà au bon endroit
    if original_msg.message_thread_id == target_thread_id:
        try:
            m = await msg.reply_text("Ce message est déjà dans le bon topic.")
            await asyncio.sleep(3)
            await m.delete()
            await msg.delete() # Supprime la commande /deplacer
        except Exception: pass
        return

    # 6. Republier le contenu
    try:
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
            await msg.reply_text("Ce type de message (ex: sticker, album) ne peut pas être déplacé.")
            return

        # 7. Supprimer l'original (et la commande)
        await original_msg.delete()
        await msg.delete() # Nettoie la commande /deplacer

    except Exception as e:
        print(f"[DEPLACER] Erreur publication/suppression : {e}")
        try:
            await msg.reply_text(f"Erreur lors du déplacement : {e}")
        except Exception: pass


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_user_id = query.from_user.id
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM edit_state WHERE admin_id = ?", (admin_user_id,))
            await db.commit()
    except Exception as e:
        print(f"[ON BUTTON CLICK - DB CLEAN] {e}")
    await query.answer()
    data = query.data
    action, report_id = data.split("|", 1)
    info = None
    try:
        async with aiosqlite.connect(DB_NAME) as db:
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
                    await query.edit_message_text("🚫 Déjà traité / introuvable.")
                except Exception: pass
                return
            
            # --- CAS REJET ---
            if action == "REJECT":
                try:
                    await query.edit_message_text("❌ Supprimé, non publié.")
                except Exception: pass
                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()
                return

            # --- CAS MODIFIER ---
            elif action == "EDIT":
                current_text = info.get("text", "")
                try:
                    await db.execute(
                        "INSERT OR REPLACE INTO edit_state (admin_id, report_id) VALUES (?, ?)", 
                        (admin_user_id, report_id)
                    )
                    await db.commit()
                    await query.edit_message_text(
                        f"✏️ **Modification en cours...**\n\n**Texte actuel :**\n`{current_text}`\n\nEnvoyez le nouveau texte. (ou envoyez /cancel pour annuler)",
                        reply_markup=None, parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"[EDIT BUTTON] {e}")
                return

            # --- CAS APPROUVE ---
            if action == "APPROVE":
                files = info["files"]
                text = (info["text"] or "").strip()
                caption_for_public = text if text else None
                text_lower = text.lower() if text else ""
                
                # Les listes de mots-clés sont maintenant globales (en haut)
                if any(word in text_lower for word in accident_keywords):
                    target_thread_id = PUBLIC_TOPIC_VIDEOS_ID
                elif any(word in text_lower for word in radar_keywords):
                    target_thread_id = PUBLIC_TOPIC_RADARS_ID
                else:
                    target_thread_id = PUBLIC_TOPIC_GENERAL_ID # Par défaut dans Général

                # --- Publication ---
                try:
                    if not files:
                        if text:
                            await context.bot.send_message(
                                chat_id=PUBLIC_GROUP_ID, text=text,
                                message_thread_id=target_thread_id
                            )
                            await query.edit_message_text("✅ Publié (texte).")
                        else:
                            await query.edit_message_text("❌ Rien à publier (vide).")
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
                        await query.edit_message_text("✅ Publié dans le groupe public.")
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
                        await query.edit_message_text("✅ Publié (album) dans le groupe public.")
                except Exception as e:
                    print(f"[ERREUR PUBLICATION] {e}")
                    try:
                        await query.edit_message_text(f"⚠️ Erreur publication: {e}")
                    except Exception: pass
                    return
                
                # --- Notification à l'utilisateur ---
                try:
                    user_chat_id_str, _ = report_id.split("_", 1)
                    user_chat_id = int(user_chat_id_str)
                    await context.bot.send_message(
                        chat_id=user_chat_id,
                        text="✅ Ton signalement a été publié dans le canal @AccidentsFR."
                    )
                except Exception as e:
                    print(f"[ERREUR NOTIFY USER] {e} (User: {user_chat_id})")
                
                # --- Nettoyage BDD ---
                await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
                await db.commit()
                return
    except Exception as e:
        print(f"[ERREUR ON_BUTTON_CLICK - GLOBAL] {e}")

# =========================================================
# WORKER D'ENVOI VERS ADMIN + CLEANER MÉMOIRE
# =========================================================

async def worker_loop(application):
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
                if int(now) % 3600 == 0: 
                    await db.execute("DELETE FROM edit_state")
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

# =========================================================
# KEEP ALIVE (Render Free)
# =========================================================

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
    flask_app.run(host="0.0.0.0", port=10000, debug=False)

# =========================================================
# MAIN
# =========================================================

def start_bot_once():
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- HANDLERS (dans le bon ordre) ---
    
    # 1. Clics sur les boutons (Admin)
    app.add_handler(CallbackQueryHandler(on_button_click))
    
    # 2. Commande /cancel (Admin)
    app.add_handler(CommandHandler(
        "cancel",
        handle_admin_cancel,
        filters=filters.Chat(ADMIN_GROUP_ID)
    ))

    # 3. NOUVEAU : Commande /deplacer (Admin)
    app.add_handler(CommandHandler(
        "deplacer",
        handle_deplacer,
        filters=filters.Chat(PUBLIC_GROUP_ID) & filters.REPLY
    ))

    # 4. Messages texte pour la modification (Admin)
    app.add_handler(MessageHandler(
        filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND, 
        handle_admin_edit
    ))
    
    # 5. Tous les autres messages (incluant les messages de service à supprimer)
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND, 
        handle_user_message
    ))
    # --- FIN DES HANDLERS ---

    async def post_init(application: ContextTypes.DEFAULT_TYPE):
        try:
            await init_db() 
            print("Connexion BDD partagée... [retirée, c'est mieux ainsi]")
            asyncio.create_task(worker_loop(application))
            asyncio.create_task(cleaner_loop())
        except Exception as e:
            print(f"[ERREUR POST_INIT] {e}")

    app.post_init = post_init
    print("🚀 Bot démarré, en écoute…")
    app.run_polling(
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )

if __name__ == "__main__":
    start_bot_once()
