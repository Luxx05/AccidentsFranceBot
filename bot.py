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

ADMIN_GROUP_ID = -1003294631521    # groupe privé admin
PUBLIC_GROUP_ID = -1003245719893   # groupe public

FLOOD_WINDOW = 5  # secondes mini entre 2 envois texte/solo du même user
CLEAN_MAX_AGE_ALBUM = 15 * 60      # 15 min pour albums pas finalisés
CLEAN_MAX_AGE_PENDING = 24 * 60 * 60  # 24h pour pending pas approuvés

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 30

KEEP_ALIVE_URL = "https://accidentsfrancebot.onrender.com"  # ton URL Render

PENDING = {}       # report_id -> {"files":[{type,file_id}], "text":..., "user_name":..., "ts":...}
TEMP_ALBUMS = {}   # media_group_id -> {files:[], text:"", user_name:"", ts:..., chat_id:..., done:bool}
LAST_MSG_TIME = {} # anti-spam user_id -> timestamp
QUEUE = asyncio.Queue()  # jobs à envoyer dans ADMIN_GROUP_ID


# ================== UTILS ==================

def _now():
    return time.time()


def _anti_spam(user_id: int, media_group_id) -> bool:
    """
    True => bloquer
    False => autoriser

    Si media_group_id != None => c'est un album Telegram (plusieurs photos en 1 envoi)
    On NE bloque pas les albums.
    """
    if media_group_id is not None:
        return False

    now = _now()
    last = LAST_MSG_TIME.get(user_id, 0)
    if now - last < FLOOD_WINDOW:
        return True
    LAST_MSG_TIME[user_id] = now
    return False


def _build_admin_preview(user_name: str, text: str | None, album: bool) -> str:
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
    Envoie le signalement dans le groupe admin :
    1. message texte + boutons
    2. médias ensuite
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
    Publie dans le groupe public après APPROVE.
    """
    files = info["files"]
    text = (info["text"] or "").strip()
    caption_for_public = text if text else None

    if not files:
        if text:
            await context.bot.send_message(
                chat_id=PUBLIC_GROUP_ID,
                text=text
            )
        return

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


# ================== LOOPS (même event loop que Telegram) ==================

async def worker_loop(application):
    """
    Lit QUEUE et envoie les signalements dans ADMIN_GROUP_ID.
    Cette loop tourne dans la même boucle asyncio que Telegram (grâce à post_init).
    """
    print("👷 Worker démarré")
    while True:
        try:
            report_id = await QUEUE.get()
            data = PENDING.get(report_id)
            if data:
                # passe par application.bot (même loop que Telegram)
                await _send_to_admin(application, report_id, data)
            QUEUE.task_done()
        except Exception as e:
            print("Erreur worker_loop:", e)
        await asyncio.sleep(0.1)


async def cleaner_loop():
    """
    Nettoyage mémoire récurrent.
    Tourne aussi dans l'event loop Telegram.
    """
    print("🧼 Cleaner démarré")
    while True:
        now = _now()

        # TEMP_ALBUMS old
        for gid, album in list(TEMP_ALBUMS.items()):
            if now - album["ts"] > CLEAN_MAX_AGE_ALBUM:
                TEMP_ALBUMS.pop(gid, None)

        # PENDING old
        for rid, data in list(PENDING.items()):
            created_ts = data.get("ts", now)
            if now - created_ts > CLEAN_MAX_AGE_PENDING:
                PENDING.pop(rid, None)

        # LAST_MSG_TIME purge
        for uid, last_ts in list(LAST_MSG_TIME.items()):
            if now - last_ts > 3600:
                LAST_MSG_TIME.pop(uid, None)

        await asyncio.sleep(60)


# ================== HANDLERS ==================

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    # 1️⃣ Ignorer les messages venant du groupe public (ils restent visibles, pas modérés)
    if msg.chat.id == PUBLIC_GROUP_ID:
        return

    # 2️⃣ Anti-flood / anti-spam : évite l’envoi de plusieurs médias d’un coup
    if anti_spam(user.id, msg.media_group_id):
        try:
            await msg.reply_text("⚠️ Calme un peu, envoie pas tout d’un coup 🙏")
        except:
            pass
        return

    # 3️⃣ Récupérer le texte du message
    piece_text = (msg.caption or msg.text or "").strip()

    # 4️⃣ Créer un identifiant utilisateur (optionnel, sinon reste anonyme)
    user_name = f"@{user.username}" if user.username else "anonyme"

    # 5️⃣ Construire le texte envoyé au groupe admin
    text_admin = (
        f"📩 **Nouveau signalement**\n"
        f"👤 {user_name}\n"
        f"{piece_text}"
    )

    # 6️⃣ Envoyer le message dans le groupe admin avec boutons
    try:
        if msg.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=msg.photo[-1].file_id,
                caption=text_admin,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Publier", callback_data="approve")],
                    [InlineKeyboardButton("❌ Supprimer", callback_data="reject")]
                ]),
            )
        elif msg.video:
            await context.bot.send_video(
                chat_id=ADMIN_GROUP_ID,
                video=msg.video.file_id,
                caption=text_admin,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Publier", callback_data="approve")],
                    [InlineKeyboardButton("❌ Supprimer", callback_data="reject")]
                ]),
            )
        else:
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=text_admin,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Publier", callback_data="approve")],
                    [InlineKeyboardButton("❌ Supprimer", callback_data="reject")]
                ]),
            )
    except Exception as e:
        print(f"Erreur envoi admin : {e}")

    # 7️⃣ Confirmer à l’utilisateur que c’est reçu
    try:
        await msg.reply_text("✅ Reçu. Vérif avant publication.")
    except:
        pass


# ================== KEEP ALIVE ==================

def keep_alive():
    while True:
        try:
            requests.get(KEEP_ALIVE_URL)
        except Exception:
            pass
        time.sleep(600)


# ================== MAIN ==================

# verrou global anti double démarrage
BOT_ALREADY_RUNNING = False

def start_bot_once():
    global BOT_ALREADY_RUNNING
    if BOT_ALREADY_RUNNING:
        print("⚠️ Bot déjà lancé, on skip pour éviter le conflit Telegram.")
        return
    BOT_ALREADY_RUNNING = True

    # thread keep_alive (ping Render)
    threading.Thread(target=keep_alive, daemon=True).start()

    # build Telegram app
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    app.add_handler(CallbackQueryHandler(on_button_click))

    # lancer worker_loop + cleaner_loop dans la même event loop que Telegram
    async def post_init(application: ContextTypes.DEFAULT_TYPE):
        asyncio.create_task(worker_loop(application))
        asyncio.create_task(cleaner_loop())

    app.post_init = post_init

    print("🚀 Bot démarré, en écoute…")
    app.run_polling(
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT
    )

# ================== SERVEUR WEB POUR RENDER ==================
from flask import Flask
import threading
import os

app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "✅ Bot AccidentsFrance en ligne"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()
# ============================================================

if __name__ == "__main__":
    start_bot_once()




