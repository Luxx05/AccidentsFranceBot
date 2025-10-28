import os
import time
import threading
import asyncio
import queue
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
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")  # token du bot Telegram
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1003294631521"))      # groupe privé modération
PUBLIC_GROUP_ID = int(os.getenv("PUBLIC_GROUP_ID", "-1003245719893"))    # groupe public affichage
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "https://accidentsfrancebot.onrender.com")

# anti-spam cooldown par user (secondes)
SPAM_COOLDOWN = 4

# nettoyage mémoire (secondes)
CLEAN_MAX_AGE_PENDING = 3600  # 1h

# paramètres polling Telegram
POLL_INTERVAL = 2.0   # secondes entre 2 getUpdates
POLL_TIMEOUT = 30     # timeout long-polling

# =========================================================
# STOCKAGE EN MÉMOIRE
# =========================================================

# Dernier message vu par chaque user => anti-flood
LAST_MSG_TIME = {}  # {user_id: timestamp_last_message}

# PENDING = signalements en attente de modération
#   key: report_id
#   val: {
#       "text": str,
#       "files": [ {"type": "photo"/"video", "file_id": "xxx"} ],
#   }
PENDING = {}

# TEMP_ALBUMS = pour reconstruire les albums envoyés en plusieurs messages
#   key: media_group_id
#   val: {
#       "files": [...],
#       "text": "...",
#       "user_name": "...",
#       "ts": last_piece_timestamp,
#       "chat_id": user_chat_id,
#       "done": False
#   }
TEMP_ALBUMS = {}

# file d'attente interne : ce que les gens envoient → à envoyer au groupe admin
REVIEW_QUEUE = queue.Queue()

# pour ne pas traiter 2 fois le même album
ALREADY_FORWARDED_ALBUMS = set()  # {report_id}


# =========================================================
# OUTILS
# =========================================================

def _now() -> float:
    return time.time()


def _is_spam(user_id: int, media_group_id) -> bool:
    """Retourne True si on doit calmer la personne (trop rapide).
    On laisse passer les albums (media_group_id non None) pour éviter de casser l'upload multi-fichiers.
    """
    if media_group_id:
        return False

    t = _now()
    last = LAST_MSG_TIME.get(user_id, 0)
    if t - last < SPAM_COOLDOWN:
        LAST_MSG_TIME[user_id] = t  # on met à jour quand même
        return True
    LAST_MSG_TIME[user_id] = t
    return False


def _is_from_public_group(chat_id: int) -> bool:
    """True si le message vient du groupe public (donc déjà visible)."""
    return chat_id == PUBLIC_GROUP_ID


def _make_admin_preview(user_name: str, text: str | None, is_album: bool) -> str:
    head = "📩 Nouveau signalement" + (" (album)" if is_album else "")
    who = f"\n👤 {user_name}"
    body = f"\n\n{text}" if text else ""
    return head + who + body


def _build_mod_keyboard(report_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
        ]]
    )


# =========================================================
# GESTION DU CONTENU UTILISATEUR
# =========================================================

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    chat_id = msg.chat_id
    media_group_id = msg.media_group_id  # album id ou None

            # === Anti-spam / anti-flood groupe public ===
    # On n'agit QUE dans le groupe public
    if chat_id == PUBLIC_GROUP_ID:
        user_id = user.id
        text_raw = (msg.text or msg.caption or "").strip()
        text = text_raw.lower()

        now_ts = _now()
        user_state = SPAM_COUNT.get(user_id, {"count": 0, "last": 0})

        # 1. flood = plusieurs messages trop rapprochés
        # on réutilise _is_spam pour détecter le spam
        flood = _is_spam(user_id, media_group_id)

        # 2. message nonsense = genre "dfjhdfjhdjfhdjfh"
        # règle : +5 chars, et quasi pas de voyelles
        gibberish = False
        if len(text) >= 5:
            consonnes = sum(1 for c in text if c in "bcdfghjklmnpqrstvwxyz")
            voyelles = sum(1 for c in text if c in "aeiouy")
            ratio = consonnes / (voyelles + 1)
            if ratio > 5:  # genre 20 consonnes / 1 voyelle -> du bruit
                gibberish = True

        # si spam ou message nonsense => on agit
        if flood or gibberish:
            # tentative suppression du message dans le groupe
            try:
                await msg.delete()
            except Exception as e:
                print(f"[ANTISPAM] delete fail: {e}")

            # update compteur spam user
            # si le dernier spam était y'a +10 sec, on reset le compteur
            if now_ts - user_state["last"] > 10:
                user_state["count"] = 0

            user_state["count"] += 1
            user_state["last"] = now_ts
            SPAM_COUNT[user_id] = user_state

            # si trop relou -> mute temporaire
            if user_state["count"] >= MUTE_THRESHOLD:
                # reset compteur pour pas re-muter en boucle
                SPAM_COUNT[user_id] = {"count": 0, "last": now_ts}

                until_ts = int(now_ts + MUTE_DURATION_SEC)

                try:
                    # restrict_chat_member version python-telegram-bot 20+/21+
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

                # log dans le groupe admin pour que tu voies qui c'est
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_GROUP_ID,
                        text=f"🔇 {user_id} mute {MUTE_DURATION_SEC//60} min pour spam."
                    )
                except Exception as e:
                    print(f"[ANTISPAM] admin notify fail: {e}")

            # on arrête là : on NE traite pas ce message pour modération
            return
    # === fin anti-spam ===


    # 🚫 ignore tout message venant du groupe public
    if chat_id == PUBLIC_GROUP_ID:
        return

    # anti-spam simple (sauf si album)
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
        new_pending = {
            "files": [],
            "text": piece_text,
            "from_public_group": False,
        }
        if media_type and file_id:
            new_pending["files"].append({"type": media_type, "file_id": file_id})

        PENDING[report_id] = new_pending

        REVIEW_QUEUE.put({
            "report_id": report_id,
            "preview_text": _make_admin_preview(user_name, piece_text, is_album=False),
            "files": new_pending["files"],
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
            "user_name": user_name,
            "chat_id": chat_id,
            "ts": _now(),
            "done": False,
            "from_public_group": False,
        }
        album = TEMP_ALBUMS[media_group_id]

    if media_type and file_id:
        album["files"].append({"type": media_type, "file_id": file_id})

    if piece_text and not album["text"]:
        album["text"] = piece_text

    album["ts"] = _now()

    asyncio.create_task(finalize_album_later(media_group_id, context, msg))



async def finalize_album_later(media_group_id, context: ContextTypes.DEFAULT_TYPE, original_msg):
    """Attend 0.5s pour laisser Telegram envoyer toutes les pièces d'un album,
    puis construit un seul report propre.
    """
    await asyncio.sleep(0.5)

    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        return
    if album["done"]:
        return
    album["done"] = True  # pour ne pas renvoyer deux fois

    report_id = f"{album['chat_id']}_{media_group_id}"
    ALREADY_FORWARDED_ALBUMS.add(report_id)

    PENDING[report_id] = {
        "files": album["files"],
        "text": album["text"],
        "from_public_group": album["from_public_group"],
    }

    # push vers modération
    REVIEW_QUEUE.put({
        "report_id": report_id,
        "preview_text": _make_admin_preview(album["user_name"], album["text"], is_album=True),
        "files": album["files"],
    })

    # mini accusé pour l'utilisateur
    try:
        await original_msg.reply_text("✅ Reçu (album). Vérif avant publication.")
    except Exception:
        pass

    # on peut nettoyer direct pour pas remplir la RAM
    TEMP_ALBUMS.pop(media_group_id, None)


# =========================================================
# ENVOI DANS LE GROUPE ADMIN + MODÉRATION
# =========================================================

async def send_report_to_admin(application, report_id: str, preview_text: str, files: list[dict]):
    """
    Envoie dans le groupe admin :
    1) un bloc texte + boutons
    2) les médias reçus (si plusieurs)
    """
    kb = _build_mod_keyboard(report_id)

    # 1. message principal (toujours texte + boutons)
    try:
        await application.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=preview_text,
            reply_markup=kb,
        )
    except Exception as e:
        print(f"[ADMIN SEND] erreur (texte) : {e}")

    # 2. médias
    if not files:
        return

    if len(files) == 1:
        m = files[0]
        try:
            if m["type"] == "photo":
                await application.bot.send_photo(
                    chat_id=ADMIN_GROUP_ID,
                    photo=m["file_id"],
                    caption=None,
                )
            else:
                await application.bot.send_video(
                    chat_id=ADMIN_GROUP_ID,
                    video=m["file_id"],
                    caption=None,
                )
        except Exception as e:
            print(f"[ADMIN SEND] erreur single media : {e}")
        return

    # plusieurs médias -> album media_group
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


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    L'admin clique ✅ ou ❌
    """
    query = update.callback_query
    await query.answer()

    try:
        data = query.data  # "APPROVE|<report_id>" ou "REJECT|<report_id>"
        action, rid = data.split("|", 1)
    except Exception:
        return

    info = PENDING.get(rid)
    if not info:
        # déjà traité ou nettoyé
        await safe_edit(query, "⛔ Déjà traité ou introuvable.")
        return

    # si rejet
    if action == "REJECT":
        await safe_edit(query, "❌ Supprimé, non publié.")
        PENDING.pop(rid, None)
        return

    # si approbation
        
        if action == "APPROVE":
        files = info["files"]
        text = (info["text"] or "").strip()
        caption_for_public = text if text else None

        # --- 1. choisir le topic où poster dans le groupe PUBLIC ---
        text_lower = text.lower()

        PUBLIC_TOPIC_VIDEOS_ID = 224  # 🎥 Vidéos & Dashcams
        PUBLIC_TOPIC_RADARS_ID = 222  # 📍 Radars & Signalements

        if any(keyword in text_lower for keyword in ["radar", "contrôle", "controle", "contrôle", "voiture banalisée", "banalisée", "voiture radar", "flash", "laser", "laser mobile", "danger"]):
            target_thread_id = PUBLIC_TOPIC_RADARS_ID
        else:
            target_thread_id = PUBLIC_TOPIC_VIDEOS_ID

        # --- 2. cas : juste du texte ---
        if not files:
            if text:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID,
                    text=text,
                    message_thread_id=target_thread_id
                )
                await safe_edit(query, "✅ Publié (texte).")
            else:
                await safe_edit(query, "❌ Rien à publier (vide).")
            PENDING.pop(report_id, None)
            return

        # --- 3. cas : un seul média (photo OU vidéo) ---
        if len(files) == 1:
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

            await safe_edit(query, "✅ Publié dans le groupe public (classé).")
            PENDING.pop(report_id, None)
            return

        # --- 4. cas : plusieurs médias (album) ---
        media_group = []
        for i, m in enumerate(files):
            if m["type"] == "photo":
                media_group.append(
                    InputMediaPhoto(
                        media=m["file_id"],
                        caption=caption_for_public if i == 0 else None
                    )
                )
            else:
                media_group.append(
                    InputMediaVideo(
                        media=m["file_id"],
                        caption=caption_for_public if i == 0 else None
                    )
                )

        # ⚠️ send_media_group gère pas message_thread_id dans toutes les versions de la lib.
        # donc on fait 2 étapes:
        # 1. on envoie le 1er média nous-mêmes dans le bon topic
        # 2. on envoie le reste en réponse dans ce même topic

        first = media_group[0]
        rest = media_group[1:]

        # envoyer le premier média manuellement
        if isinstance(first, InputMediaPhoto):
            sent_msg = await context.bot.send_photo(
                chat_id=PUBLIC_GROUP_ID,
                photo=first.media,
                caption=first.caption,
                message_thread_id=target_thread_id
            )
        else:
            sent_msg = await context.bot.send_video(
                chat_id=PUBLIC_GROUP_ID,
                video=first.media,
                caption=first.caption,
                message_thread_id=target_thread_id
            )

        # envoyer le reste en réponse dans le même thread
        for extra in rest:
            if isinstance(extra, InputMediaPhoto):
                await context.bot.send_photo(
                    chat_id=PUBLIC_GROUP_ID,
                    photo=extra.media,
                    caption=None,
                    message_thread_id=target_thread_id,
                    reply_to_message_id=sent_msg.message_id
                )
            else:
                await context.bot.send_video(
                    chat_id=PUBLIC_GROUP_ID,
                    video=extra.media,
                    caption=None,
                    message_thread_id=target_thread_id,
                    reply_to_message_id=sent_msg.message_id
                )

        await safe_edit(query, "✅ Publié dans le groupe public (album trié).")
        PENDING.pop(report_id, None)
        return



# =========================================================
# WORKER D'ENVOI VERS ADMIN + CLEANER MÉMOIRE
# =========================================================

async def worker_loop(application):
    """Lit la REVIEW_QUEUE en boucle et envoie au groupe admin."""
    print("👷 Worker démarré")
    while True:
        try:
            item = REVIEW_QUEUE.get(timeout=1)
        except queue.Empty:
            await asyncio.sleep(0.1)
            continue

        rid = item["report_id"]
        preview = item["preview_text"]
        files = item["files"]

        await send_report_to_admin(application, rid, preview, files)


async def cleaner_loop():
    """Nettoie PENDING, TEMP_ALBUMS, LAST_MSG_TIME pour pas exploser la RAM."""
    print("🧽 Cleaner démarré")
    while True:
        now = _now()

        # vire les PENDING trop vieux
        for rid, data in list(PENDING.items()):
            # pas stocké de timestamp par report, donc on fait un approximatif :
            # on vire tout ce qui dépasse CLEAN_MAX_AGE_PENDING via heuristique :
            # si rid pas dans ALREADY_FORWARDED_ALBUMS ni récent, etc.
            # simplifié : si plus vieux que 1h via rien d'autre → on n'a pas
            # l'info directe. On fait un compromis simple : rien ici, ou on supprime rien
            # pour éviter bug. On va juste garder CLEAN_MAX_AGE_PENDING en blind delete.
            pass

        # purge brute des PENDING trop vieux après 1h (simple)
        # => dans un vrai système on stockerait un timestamp dans PENDING pour chaque rid
        # Ici on fait safe : si plus de CLEAN_MAX_AGE_PENDING depuis lancement,
        # pas trivial sans timestamp par report, donc on saute cette étape pour l'instant.
        # (Tu peux l'ajouter plus tard avec PENDING[rid]["created_ts"] etc.)

        # purge LAST_MSG_TIME (anti-spam) si vieux >1h
        for uid, last_ts in list(LAST_MSG_TIME.items()):
            if now - last_ts > 3600:
                LAST_MSG_TIME.pop(uid, None)

        # purge TEMP_ALBUMS restants bloqués
        for mgid, album in list(TEMP_ALBUMS.items()):
            if now - album["ts"] > 60:
                TEMP_ALBUMS.pop(mgid, None)

        await asyncio.sleep(60)


# =========================================================
# KEEP ALIVE (Render Free)
# =========================================================

def keep_alive():
    """Ping périodiquement l'URL Render pour éviter l'endormissement trop long."""
    while True:
        try:
            requests.get(KEEP_ALIVE_URL, timeout=5)
        except Exception:
            pass
        time.sleep(600)  # toutes les 10 min


# mini serveur Flask juste pour avoir un port ouvert sur Render Free
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def hello():
    return "OK - bot alive"


def run_flask():
    # Render Free attend que le service écoute un port.
    # Flask écoute le port 10000 par ex.
    flask_app.run(host="0.0.0.0", port=10000, debug=False)


# =========================================================
# MAIN
# =========================================================

def start_bot_once():
    """
    Lance :
    - keep_alive thread
    - Flask thread (pour Render, port ouvert)
    - l'app Telegram + worker_loop + cleaner_loop
    - polling (avec anti-crash léger)
    """
    # thread "keep alive"
    threading.Thread(target=keep_alive, daemon=True).start()

    # thread Flask
    threading.Thread(target=run_flask, daemon=True).start()

    # build application Telegram
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # HANDLERS
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    app.add_handler(CallbackQueryHandler(on_button_click))

    # on lance worker_loop + cleaner_loop DANS la même boucle async du bot
    async def post_init(application: ContextTypes.DEFAULT_TYPE):
        asyncio.create_task(worker_loop(application))
        asyncio.create_task(cleaner_loop())

    app.post_init = post_init

    print("🚀 Bot démarré, en écoute…")

    # run_polling bloque tant que le bot tourne
    # On laisse Telegram.polling lever les exceptions si conflit (=bot lancé ailleurs)
    app.run_polling(
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )


if __name__ == "__main__":
    start_bot_once()




