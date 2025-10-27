import os
import time
import asyncio
import threading
import requests

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
)

# ================== CONFIG ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_GROUP_ID = -1003294631521    # groupe privé admin (là où tu valides)
PUBLIC_GROUP_ID = -1003245719893   # groupe public (là où ça publie)

FLOOD_WINDOW = 5  # anti spam: sec mini entre 2 envois d'un même user
CLEAN_MAX_AGE_ALBUM = 15 * 60      # 15 min pour les albums pas finalisés
CLEAN_MAX_AGE_PENDING = 24 * 60 * 60  # 24h pour les signalements pas approuvés
POLL_INTERVAL = 3.0  # délai entre poll, pour calmer Telegram
POLL_TIMEOUT = 30    # timeout long avant erreur
KEEP_ALIVE_URL = "https://accidentsfrancebot.onrender.com"  # <- METS ICI TON URL RENDER

# Mémoire runtime
PENDING = {}       # report_id -> {"files":[{type,file_id}], "text":..., "ts":...}
TEMP_ALBUMS = {}   # media_group_id -> {files:[], text:"", user_name:"", ts:..., chat_id:..., done:bool}
LAST_MSG_TIME = {} # user_id -> last_timestamp (anti-spam)

# File d'attente des jobs à envoyer dans l'admin
QUEUE = asyncio.Queue()

# ================== UTILS ==================


def _now():
    return time.time()


def _anti_spam(user_id: int) -> bool:
    """
    retourne True si on doit BLOQUER le user (trop rapide),
    False si ok.
    """
    now = _now()
    last = LAST_MSG_TIME.get(user_id, 0)
    if now - last < FLOOD_WINDOW:
        return True
    LAST_MSG_TIME[user_id] = now
    return False


def _build_admin_preview(user_name: str, text: str | None, album: bool) -> str:
    """
    Texte qui part dans le groupe admin.
    """
    if album:
        head = "📩 Nouveau signalement (album)"
    else:
        head = "📩 Nouveau signalement"

    preview = f"{head}\n👤 {user_name}"
    if text:
        preview += f"\n\n{text.strip()}"
    return preview


def _build_keyboard(report_id: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
        ]]
    )


async def _send_to_admin(context: ContextTypes.DEFAULT_TYPE, report_id: str, data: dict):
    """
    Envoie le signalement dans le groupe admin:
    - message texte + boutons
    - puis médias (photos/vidéos) après
    """
    files = data["files"]
    text = data["text"]
    user_name = data["user_name"]

    admin_preview = _build_admin_preview(user_name, text, album=(len(files) > 1))

    # 1. message texte avec boutons
    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_preview,
            reply_markup=_build_keyboard(report_id)
        )
    except Exception as e:
        print("Erreur send_message admin:", e)

    # 2. médias
    if not files:
        return

    if len(files) == 1:
        m = files[0]
        try:
            if m["type"] == "photo":
                await context.bot.send_photo(
                    chat_id=ADMIN_GROUP_ID,
                    photo=m["file_id"],
                    caption=None
                )
            else:
                await context.bot.send_video(
                    chat_id=ADMIN_GROUP_ID,
                    video=m["file_id"],
                    caption=None
                )
        except Exception as e:
            print("Erreur send single media admin:", e)
    else:
        media_group = []
        for m in files:
            if m["type"] == "photo":
                media_group.append(InputMediaPhoto(media=m["file_id"]))
            else:
                media_group.append(InputMediaVideo(media=m["file_id"]))
        try:
            await context.bot.send_media_group(
                chat_id=ADMIN_GROUP_ID,
                media=media_group
            )
        except Exception as e:
            print("Erreur send_media_group admin:", e)


async def _publish_public(context: ContextTypes.DEFAULT_TYPE, info: dict):
    """
    Quand tu cliques ✅, on balance dans le groupe public.
    Gère:
    - texte seul
    - 1 média
    - multi médias
    """
    files = info["files"]
    text = (info["text"] or "").strip()
    caption_for_public = text if text else None

    # juste texte
    if not files:
        if text:
            await context.bot.send_message(
                chat_id=PUBLIC_GROUP_ID,
                text=text
            )
        return

    # un média
    if len(files) == 1:
        m = files[0]
        if m["type"] == "photo":
            await context.bot.send_photo(
                chat_id=PUBLIC_GROUP_ID,
                photo=m["file_id"],
                caption=caption_for_public
            )
        else:
            await context.bot.send_video(
                chat_id=PUBLIC_GROUP_ID,
                video=m["file_id"],
                caption=caption_for_public
            )
        return

    # plusieurs médias -> album
    media_group = []
    for i, m in enumerate(files):
        if m["type"] == "photo":
            media_group.append(InputMediaPhoto(
                media=m["file_id"],
                caption=caption_for_public if i == 0 else None
            ))
        else:
            media_group.append(InputMediaVideo(
                media=m["file_id"],
                caption=caption_for_public if i == 0 else None
            ))
    await context.bot.send_media_group(
        chat_id=PUBLIC_GROUP_ID,
        media=media_group
    )


# ================== QUEUE WORKER / CLEANER ==================


async def worker_loop(app_context: ContextTypes.DEFAULT_TYPE):
    """
    Boucle qui lit QUEUE et envoie les signalements dans ADMIN_GROUP_ID.
    1 job = un report_id déjà prêt dans PENDING.
    """
    print("👷 Worker démarré")
    while True:
        try:
            report_id = await QUEUE.get()
            data = PENDING.get(report_id)
            if data:
                await _send_to_admin(app_context, report_id, data)
            QUEUE.task_done()
        except Exception as e:
            print("Erreur worker_loop:", e)
        await asyncio.sleep(0.1)


async def cleaner_loop():
    """
    Nettoyage mémoire:
    - vire les albums pas finalisés trop vieux
    - vire les PENDING trop vieux (optionnel après 24h)
    - vire les timestamps anti-spam très anciens
    tourne en tâche de fond
    """
    print("🧼 Cleaner démarré")
    while True:
        now = _now()

        # clean TEMP_ALBUMS
        old_albums = []
        for gid, album in list(TEMP_ALBUMS.items()):
            if now - album["ts"] > CLEAN_MAX_AGE_ALBUM:
                old_albums.append(gid)
        for gid in old_albums:
            TEMP_ALBUMS.pop(gid, None)

        # clean PENDING
        old_pending = []
        for rid, data in list(PENDING.items()):
            created_ts = data.get("ts", now)
            if now - created_ts > CLEAN_MAX_AGE_PENDING:
                old_pending.append(rid)
        for rid in old_pending:
            PENDING.pop(rid, None)

        # clean LAST_MSG_TIME (anti-spam map)
        for uid, last_ts in list(LAST_MSG_TIME.items()):
            if now - last_ts > 3600:  # 1h sans parler -> on purge
                LAST_MSG_TIME.pop(uid, None)

        await asyncio.sleep(60)


# ================== HANDLERS TELEGRAM ==================


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reçoit n'importe quoi d'un utilisateur vers le bot:
    - texte
    - 1 photo / 1 vidéo
    - album (plusieurs médias)
    Et prépare le signalement.
    """
    msg = update.message
    user = msg.from_user

    # anti flood: si user spam trop vite, on refuse
    if _anti_spam(user.id):
        try:
            await msg.reply_text("⏳ Calme un peu, envoie pas tout d'un coup 🙏")
        except Exception:
            pass
        return

    # contenu texte du message ou caption
    piece_text = (msg.caption or msg.text or "").strip()
    user_name = f"@{user.username}" if user.username else "anonyme"

    # quel média ?
    if msg.video:
        media_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        media_type = "photo"
        file_id = msg.photo[-1].file_id  # qualité max
    else:
        media_type = "text"
        file_id = None

    media_group_id = msg.media_group_id  # None si pas album

    # CAS 1 : pas un album => on push direct en PENDING + QUEUE
    if media_group_id is None:
        report_id = f"{msg.chat_id}_{msg.id}"

        PENDING[report_id] = {
            "files": [],
            "text": piece_text,
            "user_name": user_name,
            "ts": _now(),
        }

        if media_type in ["photo", "video"]:
            PENDING[report_id]["files"].append({
                "type": media_type,
                "file_id": file_id,
            })

        # balance le job pour l'admin
        await QUEUE.put(report_id)

        # répond à l'utilisateur
        try:
            await msg.reply_text("✅ Reçu. Vérif avant publication.")
        except Exception:
            pass

        return

    # CAS 2 : album => on build TEMP_ALBUMS
    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        TEMP_ALBUMS[media_group_id] = {
            "files": [],
            "text": piece_text,
            "user_name": user_name,
            "ts": _now(),
            "chat_id": msg.chat_id,
            "done": False,
        }
        album = TEMP_ALBUMS[media_group_id]

    # ajoute ce média
    if media_type in ["photo", "video"]:
        album["files"].append({
            "type": media_type,
            "file_id": file_id,
        })

    # stocke texte si pas encore là
    if piece_text and not album["text"]:
        album["text"] = piece_text

    album["ts"] = _now()

    # déclenche la finalisation d'album (async)
    asyncio.create_task(finalize_album_later(media_group_id, context, msg))


async def finalize_album_later(media_group_id, context: ContextTypes.DEFAULT_TYPE, original_msg):
    """
    Après réception d'un album (plusieurs messages Telegram avec le même media_group_id),
    on attend un poil, on ne l'envoie à l'admin QU'UNE FOIS,
    et on push le job dans la QUEUE.
    """
    await asyncio.sleep(0.5)

    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        return

    if album.get("done"):
        return

    album["done"] = True

    report_id = f"{album['chat_id']}_{media_group_id}"

    PENDING[report_id] = {
        "files": album["files"],
        "text": album["text"],
        "user_name": album["user_name"],
        "ts": _now(),
    }

    # file d'attente pour envoi admin
    await QUEUE.put(report_id)

    # réponse à l'utilisateur une seule fois
    try:
        await original_msg.reply_text("✅ Reçu (album). Vérif avant publication.")
    except Exception:
        pass

    # on vide l'album de la mémoire
    TEMP_ALBUMS.pop(media_group_id, None)


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Quand toi tu cliques ✅ Publier ou ❌ Supprimer.
    """
    query = update.callback_query
    await query.answer()

    data = query.data  # "APPROVE|xxx" ou "REJECT|xxx"
    action, report_id = data.split("|", 1)

    info = PENDING.get(report_id)
    if not info:
        await safe_edit(query, "⛔ Déjà traité / introuvable.")
        return

    if action == "REJECT":
        await safe_edit(query, "❌ Supprimé. Non publié.")
        PENDING.pop(report_id, None)
        return

    if action == "APPROVE":
        # publier dans PUBLIC_GROUP_ID
        await _publish_public(context, info)
        await safe_edit(query, "✅ Publié.")
        PENDING.pop(report_id, None)


async def safe_edit(query, new_text: str):
    """
    Essaie de remplacer le message admin par "✅ Publié" ou "❌ Supprimé".
    """
    try:
        await query.edit_message_caption(caption=new_text)
    except Exception:
        try:
            await query.edit_message_text(text=new_text)
        except Exception:
            pass


# ================== KEEP ALIVE (Render Free) ==================

def keep_alive():
    while True:
        try:
            requests.get(KEEP_ALIVE_URL)
        except Exception:
            pass
        time.sleep(600)  # ping toutes les 10 min


# ================== MAIN LOOP ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    app.add_handler(CallbackQueryHandler(on_button_click))

    # On lance les tâches de fond (worker + cleaner)
    async def startup():
        # lancer worker (file d'attente admin)
        asyncio.create_task(worker_loop(app))
        # lancer cleaner mémoire
        asyncio.create_task(cleaner_loop())

    # On démarre keep_alive dans un thread séparé (ping Render)
    threading.Thread(target=keep_alive, daemon=True).start()

    # Boucle anti-crash: si Telegram pète, on relance
    while True:
        try:
            print("🚀 Bot démarré, en écoute…")
            # on initialise les tâches de fond avant polling
            asyncio.run(startup())
        except RuntimeError:
            # RuntimeError "asyncio.run() cannot be called from a running event loop"
            # si ça arrive, on ignore ce startup de plus
            pass

        try:
            app.run_polling(
                poll_interval=POLL_INTERVAL,
                timeout=POLL_TIMEOUT
            )
        except Exception as e:
            print(f"[CRASH] Bot a crash: {e}")
            time.sleep(5)  # pause avant retry


if __name__ == "__main__":
    main()
