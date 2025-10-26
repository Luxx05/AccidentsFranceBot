import os
import time
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

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = -1003294631521    # Groupe privé admin
PUBLIC_GROUP_ID = -1003245719893   # Groupe public
# =====================

# On garde en mémoire ce que les gens envoient
# PENDING[report_id] = {
#   "files": [ {"type": "photo"|"video", "file_id": "..."} , ... ],
#   "text": "....",
# }
PENDING = {}

# Pour regrouper les albums (media_group_id)
# TEMP_ALBUMS[media_group_id] = {
#   "files": [...],
#   "text": "...",
#   "user_name": "...",
#   "ts": timestamp_last_msg,
# }
TEMP_ALBUMS = {}


def _now():
    return time.time()


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    # Récupérer le texte utilisateur (caption du média + message texte)
    # -> si c'est un album, chaque élément peut avoir la même caption, on fusionne
    piece_text = (msg.caption or msg.text or "").strip()

    user_name = f"@{user.username}" if user.username else "anonyme"

    # Détecter le média
    if msg.video:
        media_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        media_type = "photo"
        file_id = msg.photo[-1].file_id
    else:
        media_type = "text"
        file_id = None

    # Est-ce que c'est un album ?
    media_group_id = msg.media_group_id  # None si pas album

    # CAS 1 : pas d'album -> traitement direct
    if media_group_id is None:
        # On crée un ID unique pour ce signalement
        report_id = f"{msg.chat_id}_{msg.id}"

        # On stocke ça dans PENDING
        PENDING[report_id] = {
            "files": [],
            "text": piece_text,
        }

        if media_type in ["photo", "video"]:
            PENDING[report_id]["files"].append({
                "type": media_type,
                "file_id": file_id,
            })

        # Preview pour groupe admin
        admin_preview = f"📩 Nouveau signalement\n👤 {user_name}"
        if piece_text:
            admin_preview += f"\n\n{piece_text}"

        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
                InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
            ]]
        )

        # Envoi dans le groupe admin
        if media_type == "video":
            await context.bot.send_video(
                chat_id=ADMIN_GROUP_ID,
                video=file_id,
                caption=admin_preview,
                reply_markup=keyboard
            )
        elif media_type == "photo":
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=file_id,
                caption=admin_preview,
                reply_markup=keyboard
            )
        else:
            # juste du texte
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=admin_preview,
                reply_markup=keyboard
            )

        # Répond à l'utilisateur
        await msg.reply_text("✅ Reçu. Merci. Vérif avant publication.")
        return

    # CAS 2 : c'est un album (plusieurs médias envoyés d'un coup)
    # On groupe par media_group_id
    album = TEMP_ALBUMS.get(media_group_id)

    if album is None:
        # première pièce de cet album
        TEMP_ALBUMS[media_group_id] = {
            "files": [],
            "text": piece_text,
            "user_name": user_name,
            "ts": _now(),
        }
        album = TEMP_ALBUMS[media_group_id]

    # Ajoute ce média dans l'album
    if media_type in ["photo", "video"]:
        album["files"].append({
            "type": media_type,
            "file_id": file_id,
        })

    # si ce message a du texte et qu'on n'avait rien avant, on le garde
    if piece_text and not album["text"]:
        album["text"] = piece_text

    album["ts"] = _now()

    # On attend un tout petit peu avant d'envoyer pour être sûr d'avoir toutes les photos/vidéos de l'album.
    # Astuce : on lance un petit job async différé.
    context.application.create_task(finish_album_if_complete(media_group_id, context, msg))


async def finish_album_if_complete(media_group_id, context: ContextTypes.DEFAULT_TYPE, original_msg):
    """
    On attend un mini délai pour être sûr d'avoir tout l'album.
    Telegram envoie les médias d'un album les uns à la suite très vite.
    """
    await context.application.bot._async_tasks.create_task(_sleep_small())

    # Re-check après le délai
    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        return

    # On génère un report_id unique basé sur chat et timestamp
    report_id = f"{original_msg.chat_id}_{media_group_id}"

    # On enregistre cet album en attente d'approbation
    PENDING[report_id] = {
        "files": album["files"],      # liste de médias
        "text": album["text"],        # texte commun
    }

    # Construire l'aperçu admin
    admin_preview = f"📩 Nouveau signalement (album)\n👤 {album['user_name']}"
    if album["text"]:
        admin_preview += f"\n\n{album['text']}"

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
        ]]
    )

    # On envoie un seul bloc au groupe admin :
    files = album["files"]

    if len(files) == 1:
        # album d'un seul média = simple
        media = files[0]
        if media["type"] == "photo":
            sent = await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=media["file_id"],
                caption=admin_preview,
                reply_markup=keyboard
            )
        else:
            sent = await context.bot.send_video(
                chat_id=ADMIN_GROUP_ID,
                video=media["file_id"],
                caption=admin_preview,
                reply_markup=keyboard
            )
    else:
        # Plusieurs médias -> on envoie un media group dans l'admin
        media_group = []
        for i, m in enumerate(files):
            if m["type"] == "photo":
                media_group.append(InputMediaPhoto(
                    media=m["file_id"],
                    caption=admin_preview if i == 0 else None  # caption seulement sur le 1er
                ))
            else:
                media_group.append(InputMediaVideo(
                    media=m["file_id"],
                    caption=admin_preview if i == 0 else None
                ))

        # Telegram n'autorise pas les boutons inline directement sur un envoi "album" (media_group)
        # donc on fait 2 envois :
        # 1) l'album sans boutons
        # 2) un message texte juste après avec les boutons
        await context.bot.send_media_group(
            chat_id=ADMIN_GROUP_ID,
            media=media_group
        )

        sent = await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_preview,
            reply_markup=keyboard
        )

    # clean l'album temp
    TEMP_ALBUMS.pop(media_group_id, None)

    # Répond à l'utilisateur une seule fois (sur le dernier message)
    try:
        await original_msg.reply_text("✅ Reçu (album). Merci. Vérif avant publication.")
    except:
        pass


async def _sleep_small():
    # mini pause pour laisser le temps à Telegram d'envoyer toutes les pièces de l'album
    time.sleep(0.4)


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # "APPROVE|<report_id>" ou "REJECT|<report_id>"
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
        files = info["files"]
        text = (info["text"] or "").strip()
        text_or_none = text if text else None

        # Cas 1 : aucun média, juste texte
        if not files:
            if text:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID,
                    text=text
                )
                await safe_edit(query, "✅ Publié (texte).")
            else:
                await safe_edit(query, "❌ Rien à publier (vide).")
            PENDING.pop(report_id, None)
            return

        # Cas 2 : un seul média
        if len(files) == 1:
            m = files[0]
            if m["type"] == "photo":
                await context.bot.send_photo(
                    chat_id=PUBLIC_GROUP_ID,
                    photo=m["file_id"],
                    caption=text_or_none
                )
            else:
                await context.bot.send_video(
                    chat_id=PUBLIC_GROUP_ID,
                    video=m["file_id"],
                    caption=text_or_none
                )

            await safe_edit(query, "✅ Publié dans le groupe public.")
            PENDING.pop(report_id, None)
            return

        # Cas 3 : plusieurs médias -> envoyer un album dans le groupe public
        media_group = []
        for i, m in enumerate(files):
            if m["type"] == "photo":
                media_group.append(InputMediaPhoto(
                    media=m["file_id"],
                    caption=text_or_none if i == 0 else None
                ))
            else:
                media_group.append(InputMediaVideo(
                    media=m["file_id"],
                    caption=text_or_none if i == 0 else None
                ))

        await context.bot.send_media_group(
            chat_id=PUBLIC_GROUP_ID,
            media=media_group
        )

        await safe_edit(query, "✅ Publié (album) dans le groupe public.")
        PENDING.pop(report_id, None)


async def safe_edit(query, new_text: str):
    # essaie d'éditer comme caption (si c'était un media avec légende)
    try:
        await query.edit_message_caption(caption=new_text)
    except:
        # sinon comme texte pur
        try:
            await query.edit_message_text(text=new_text)
        except:
            pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # chaque message envoyé au bot déclenche handle_user_message
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    # clic sur ✅ / ❌
    app.add_handler(CallbackQueryHandler(on_button_click))

    app.run_polling()


if __name__ == "__main__":
    main()
