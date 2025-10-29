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
    CommandHandler, # NOUVEAU
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1003294631521"))
PUBLIC_GROUP_ID = int(os.getenv("PUBLIC_GROUP_ID", "-1003245719893"))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "https://accidentsfrancebot.onrender.com")

DB_NAME = "bot_storage.db" # Assurez-vous que c'est /var/data/bot_storage.db sur Render

SPAM_COOLDOWN = 4
MUTE_THRESHOLD = 3
MUTE_DURATION_SEC = 300

CLEAN_MAX_AGE_PENDING = 3600 * 24
CLEAN_MAX_AGE_ALBUMS = 60
CLEAN_MAX_AGE_SPAM = 3600

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 30

PUBLIC_TOPIC_VIDEOS_ID = 224
PUBLIC_TOPIC_RADARS_ID = 222

# =========================================================
# STOCKAGE EN MÉMOIRE (Volatile)
# =========================================================

LAST_MSG_TIME = {}
SPAM_COUNT = {}
TEMP_ALBUMS = {}
REVIEW_QUEUE = asyncio.Queue()
ALREADY_FORWARDED_ALBUMS = set()

# NOUVEAU : Pour suivre quel admin modifie quel signalement
# {admin_user_id: report_id}
EDITING_STATE = {}

# =========================================================
# INITIALISATION BASE DE DONNÉES
# =========================================================

async def init_db():
    print("🗃️ Initialisation de la base de données SQLite...")
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            # MODIFIÉ : Ajout de user_name
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_reports (
                    report_id TEXT PRIMARY KEY,
                    text TEXT,
                    files_json TEXT,
                    created_ts INTEGER,
                    user_name TEXT 
                )
            """)
            await db.commit()
        print("🗃️ Base de données prête.")
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

# MODIFIÉ : Ajout du bouton "Modifier"
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

    user = msg.from_user
    chat_id = msg.chat_id
    media_group_id = msg.media_group_id
    
    # === Anti-spam groupe public ===
    # (Cette section ignore déjà les messages du groupe admin)
    if chat_id == PUBLIC_GROUP_ID:
        user_id = user.id
        text_raw = (msg.text or msg.caption or "").strip()
        text = text_raw.lower()

        now_ts = _now()
        user_state = SPAM_COUNT.get(user_id, {"count": 0, "last": 0})

        # 1. flood
        flood = _is_spam(user_id, media_group_id)

        # 2. message nonsense
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
            SPAM_COUNT[user_id] = user_state

            if user_state["count"] >= MUTE_THRESHOLD:
                SPAM_COUNT[user_id] = {"count": 0, "last": now_ts}
                until_ts = int(now_ts + MUTE_DURATION_SEC)

                try:
                    await context.bot.restrict_chat_member(
                        chat_id=PUBLIC_GROUP_ID,
                        user_id=user_id,
                        permissions={
                            "can_send_messages": False,
                            "can_send_media_messages": False,
                            "can_send_other_messages": False,
                            "can_add_web_page_previews": False
                        },
                        until_date=until_ts
                    )
                except Exception as e:
                    print(f"[ANTISPAM] mute fail: {e}")

                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_GROUP_ID,
                        text=f"🔇 {user_id} mute {MUTE_DURATION_SEC//60} min pour spam."
                    )
                except Exception as e:
                    print(f"[ANTISPAM] admin notify fail: {e}")
            return
    # === fin anti-spam ===

    # 🚫 ignore tout message venant du groupe public (après anti-spam)
    # ou du groupe admin (géré par d'autres handlers)
    if chat_id == PUBLIC_GROUP_ID or chat_id == ADMIN_GROUP_ID:
        return

    # === Début traitement message privé ===
    try:
        db = context.bot_data["db"]
    except KeyError:
        print("[ERREUR] Connexion DB non trouvée dans bot_data.")
        return

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

        # MODIFIÉ : Ajout de user_name
        try:
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
            "files": [],
            "text": piece_text,
            "user_name": user_name, # user_name stocké ici
            "chat_id": chat_id,
            "ts": _now(),
            "done": False,
        }
        album = TEMP_ALBUMS[media_group_id]

    if media_type and file_id:
        album["files"].append({"type": media_type, "file_id": file_id})

    if piece_text and not album["text"]:
        album["text"] = piece_text

    album["ts"] = _now()

    asyncio.create_task(finalize_album_later(media_group_id, context, msg))


async def finalize_album_later(media_group_id, context: ContextTypes.DEFAULT_TYPE, original_msg):
    await asyncio.sleep(0.5)

    album = TEMP_ALBUMS.get(media_group_id)
    if album is None or album["done"]:
        return
    album["done"] = True
    
    try:
        db = context.bot_data["db"]
    except KeyError:
        print("[ERREUR] Connexion DB non trouvée dans bot_data (finalize_album).")
        return

    report_id = f"{album['chat_id']}_{media_group_id}"
    ALREADY_FORWARDED_ALBUMS.add(report_id)

    files_list = album["files"]
    files_json = json.dumps(files_list)
    report_text = album["text"]
    created_ts = int(_now())
    user_name = album["user_name"] # Récupération du user_name

    # MODIFIÉ : Ajout de user_name
    try:
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
        await original_msg.reply_text("✅ Reçu (album). Vérif avant publication.")
    except Exception:
        pass

    TEMP_ALBUMS.pop(media_group_id, None)


# =========================================================
# ENVOI DANS LE GROUPE ADMIN + MODÉRATION
# =========================================================

async def send_report_to_admin(application, report_id: str, preview_text: str, files: list[dict]):
    kb = _build_mod_keyboard(report_id)

    try:
        await application.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=preview_text,
            reply_markup=kb,
        )
    except Exception as e:
        print(f"[ADMIN SEND] erreur (texte) : {e}")
        return # Si le texte échoue, inutile d'envoyer les médias

    if not files:
        return

    if len(files) == 1:
        m = files[0]
        try:
            if m["type"] == "photo":
                await application.bot.send_photo(
                    chat_id=ADMIN_GROUP_ID, photo=m["file_id"]
                )
            else:
                await application.bot.send_video(
                    chat_id=ADMIN_GROUP_ID, video=m["file_id"]
                )
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
        await application.bot.send_media_group(
            chat_id=ADMIN_GROUP_ID,
            media=media_group
        )
    except Exception as e:
        print(f"[ADMIN SEND] erreur album media_group : {e}")

# NOUVEAU : Handlers pour la modification par l'admin
async def handle_admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le message texte de l'admin après qu'il ait cliqué sur 'Modifier'."""
    msg = update.message
    if not msg:
        return
        
    admin_user_id = msg.from_user.id
    report_id = EDITING_STATE.pop(admin_user_id, None)

    # Si l'admin n'était pas en train de modifier, on ignore son message
    if not report_id:
        return

    new_text = msg.text
    db = context.bot_data["db"]

    try:
        # Mettre à jour le texte en BDD
        await db.execute("UPDATE pending_reports SET text = ? WHERE report_id = ?", (new_text, report_id))
        await db.commit()

        # Recréer l'aperçu pour l'admin
        async with db.cursor() as cursor:
            await cursor.execute("SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?", (report_id,))
            row = await cursor.fetchone()
        
        if not row:
            await msg.reply_text("Erreur : Signalement introuvable après mise à jour.")
            return

        text, files_json, user_name = row
        files = json.loads(files_json)
        
        # Informer l'admin
        await msg.reply_text(f"✅ Texte mis à jour. Voici le nouvel aperçu :")
        
        # Renvoyer le bloc de modération complet
        preview_text = _make_admin_preview(user_name, text, is_album=len(files) > 1)
        await send_report_to_admin(context.application, report_id, preview_text, files)

    except Exception as e:
        print(f"[HANDLE ADMIN EDIT] {e}")
        await msg.reply_text(f"Une erreur est survenue lors de la mise à jour : {e}")

async def handle_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la commande /cancel de l'admin."""
    admin_user_id = update.message.from_user.id
    if admin_user_id in EDITING_STATE:
        EDITING_STATE.pop(admin_user_id, None)
        await update.message.reply_text("Modification annulée. Vous pouvez à nouveau utiliser les boutons.")
    else:
        await update.message.reply_text("Vous n'étiez pas en train de modifier un message.")


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # NOUVEAU : Annule l'état d'édition si l'admin clique sur un bouton
    admin_user_id = query.from_user.id
    EDITING_STATE.pop(admin_user_id, None)
    
    await query.answer() # Toujours répondre au callback rapidement

    data = query.data
    action, report_id = data.split("|", 1)

    try:
        db = context.bot_data["db"]
    except KeyError:
        print("[ERREUR] Connexion DB non trouvée dans bot_data (on_button_click).")
        return

    # --- Récupération du signalement depuis la BDD ---
    info = None
    try:
        async with db.cursor() as cursor:
            # MODIFIÉ : On récupère aussi user_name
            await cursor.execute("SELECT text, files_json, user_name FROM pending_reports WHERE report_id = ?", (report_id,))
            row = await cursor.fetchone()
            if row:
                text_from_db, files_json_from_db, user_name_from_db = row
                info = {
                    "text": text_from_db,
                    "files": json.loads(files_json_from_db),
                    "user_name": user_name_from_db # NOUVEAU
                }
    except Exception as e:
        print(f"[ERREUR DB SELECT] {e}")
        return

    if not info:
        try:
            await query.edit_message_text("🚫 Déjà traité / introuvable.")
        except Exception:
            pass
        return

    # --- CAS REJET ---
    if action == "REJECT":
        try:
            await query.edit_message_text("❌ Supprimé, non publié.")
        except Exception:
            pass
        
        try:
            await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
            await db.commit()
        except Exception as e:
            print(f"[ERREUR DB DELETE REJECT] {e}")
        return

    # --- NOUVEAU : CAS MODIFIER ---
    elif action == "EDIT":
        current_text = info.get("text", "")
        try:
            # On met l'admin en état d'édition
            EDITING_STATE[admin_user_id] = report_id
            await query.edit_message_text(
                f"✏️ **Modification en cours...**\n\n**Texte actuel :**\n`{current_text}`\n\nEnvoyez le nouveau texte. (ou envoyez /cancel pour annuler)",
                reply_markup=None # On retire les boutons
            )
        except Exception as e:
            print(f"[EDIT BUTTON] {e}")
        return # On attend le message de l'admin

    # --- CAS APPROUVE ---
    if action == "APPROVE":
        files = info["files"]
        text = (info["text"] or "").strip()
        caption_for_public = text if text else None

        # --- Choix du topic ---
        text_lower = text.lower() if text else ""
        accident_keywords = ["accident", "accrochage", "carambolage", "choc", "collision", "crash", "sortie de route", "perte de contrôle", "dashcam", "vidéo accident", "camion couché", "freinage d'urgence", "percuté"]
        radar_keywords = ["radar", "contrôle", "controle", "flash", "flashé", "laser", "jumelle", "police", "gendarmerie", "banalisée", "radar caché", "radar mobile"]

        if any(word in text_lower for word in accident_keywords):
            target_thread_id = PUBLIC_TOPIC_VIDEOS_ID
        elif any(word in text_lower for word in radar_keywords):
            target_thread_id = PUBLIC_TOPIC_RADARS_ID
        else:
            target_thread_id = PUBLIC_TOPIC_VIDEOS_ID

        # --- Publication ---
        try:
            if not files:
                if text:
                    await context.bot.send_message(
                        chat_id=PUBLIC_GROUP_ID,
                        text=text,
                        message_thread_id=target_thread_id
                    )
                    await query.edit_message_text("✅ Publié (texte).")
                else:
                    await query.edit_message_text("❌ Rien à publier (vide).")

            elif len(files) == 1:
                m = files[0]
                if m["type"] == "photo":
                    await context.bot.send_photo(
                        chat_id=PUBLIC_GROUP_ID,
                        photo=m["file_id"],
                        caption=caption_for_public,
                        message_thread_id=target_thread_id
                    )
                else:
                    await context.bot.send_video(
                        chat_id=PUBLIC_GROUP_ID,
                        video=m["file_id"],
                        caption=caption_for_public,
                        message_thread_id=target_thread_id
                    )
                await query.edit_message_text("✅ Publié dans le groupe public.")

            else:  # Album
                media_group = []
                for i, m in enumerate(files):
                    caption = caption_for_public if i == 0 else None
                    if m["type"] == "photo":
                        media_group.append(InputMediaPhoto(media=m["file_id"], caption=caption))
                    else:
                        media_group.append(InputMediaVideo(media=m["file_id"], caption=caption))
                
                await context.bot.send_media_group(
                    chat_id=PUBLIC_GROUP_ID,
                    media=media_group,
                    message_thread_id=target_thread_id
                )
                await query.edit_message_text("✅ Publié (album) dans le groupe public.")
        
        except Exception as e:
            print(f"[ERREUR PUBLICATION] {e}")
            try:
                await query.edit_message_text(f"⚠️ Erreur publication: {e}")
            except Exception:
                pass
            return

        # --- NOUVEAU : Notification à l'utilisateur ---
        try:
            user_chat_id_str, _ = report_id.split("_", 1)
            user_chat_id = int(user_chat_id_str)
            await context.bot.send_message(
                chat_id=user_chat_id,
                text="✅ Ton signalement a été publié dans le canal @Accidents_France."
            )
        except Exception as e:
            print(f"[ERREUR NOTIFY USER] {e} (User: {user_chat_id})")
        # --- Fin notification ---

        # --- Nettoyage BDD après succès ---
        try:
            await db.execute("DELETE FROM pending_reports WHERE report_id = ?", (report_id,))
            await db.commit()
        except Exception as e:
            print(f"[ERREUR DB DELETE APPROVE] {e}")
        return

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


async def cleaner_loop(db: aiosqlite.Connection):
    print("🧽 Cleaner démarré")
    while True:
        await asyncio.sleep(60)
        now = _now()
        
        try:
            # --- Nettoyage BDD (PENDING) ---
            cutoff_ts_pending = int(now - CLEAN_MAX_AGE_PENDING)
            # MODIFIÉ : Prise en compte de la nouvelle colonne
            await db.execute("DELETE FROM pending_reports WHERE created_ts < ?", (cutoff_ts_pending,))
            await db.commit()

            # --- Nettoyage mémoire (anti-spam) ---
            cutoff_ts_spam = now - CLEAN_MAX_AGE_SPAM
            for uid in list(LAST_MSG_TIME.keys()):
                if LAST_MSG_TIME[uid] < cutoff_ts_spam:
                    LAST_MSG_TIME.pop(uid, None)
            
            for uid in list(SPAM_COUNT.keys()):
                if SPAM_COUNT[uid]["last"] < cutoff_ts_spam:
                    SPAM_COUNT.pop(uid, None)

            # --- Nettoyage mémoire (albums temporaires) ---
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

    try:
        asyncio.run(init_db())
    except Exception as e:
        print(f"Échec critique de l'initialisation de la BDD. Arrêt. {e}")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- HANDLERS ---
    
    # 1. Gère les clics sur les boutons (Approuver, Rejeter, Modifier)
    app.add_handler(CallbackQueryHandler(on_button_click))
    
    # 2. NOUVEAU : Gère les réponses textuelles de l'admin (pour la modification)
    app.add_handler(MessageHandler(
        filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND, 
        handle_admin_edit
    ))
    
    # 3. NOUVEAU : Gère la commande /cancel de l'admin
    app.add_handler(CommandHandler(
        "cancel",
        handle_admin_cancel,
        filters=filters.Chat(ADMIN_GROUP_ID)
    ))

    # 4. Gère tous les autres messages (privés ou groupe public)
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND, 
        handle_user_message
    ))

    # --- Tâches de fond ---
    async def post_init(application: ContextTypes.DEFAULT_TYPE):
        try:
            db = await aiosqlite.connect(DB_NAME)
            application.bot_data["db"] = db
            print("Connexion BDD partagée établie.")
            
            asyncio.create_task(worker_loop(application))
            asyncio.create_task(cleaner_loop(db))
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
