import os
import time
import asyncio
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

# PENDING : signalements en attente validation
# PENDING[report_id] = {
#   "files": [ {"type": "photo"/"video", "file_id": "..."} ],
#   "text": "....",
# }
PENDING = {}

# TEMP_ALBUMS : réception progressive d'un album
# TEMP_ALBUMS[media_group_id] = {
#   "files": [...],
#   "text": "...",
#   "user_name": "...",
#   "ts": last_timestamp,
#   "chat_id": chat_id,
#}
TEMP_ALBUMS = {}
# ============================================


def _now():
    return time.time()


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    # texte envoyé (caption média OU message texte)
    piece_text = (msg.caption or msg.text or "").strip()
    user_name = f"@{user.username}" if user.username else "anonyme"

    # Détecter le type de contenu
    if msg.video:
        media_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        media_type = "photo"
        file_id = msg.photo[-1].file_id
    else:
        media_type = "text"
        file_id = None

    media_group_id = msg.media_group_id  # None si pas album

    # ----- CAS 1 : PAS ALBUM -----
    if media_group_id is None:
        report_id = f"{msg.chat_id}_{msg.id}"

        PENDING[report_id] = {
            "files": [],
            "text": piece_text,
        }

        if media_type in ["photo", "video"]:
            PENDING[report_id]["files"].append({
                "type": media_type,
                "file_id": file_id,
            })

        # message aperçu pour admin
        admin_preview = f"📩 Nouveau signalement\n👤 {user_name}"
        if piece_text:
            admin_preview += f"\n\n{piece_text}"

        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
                InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
            ]]
        )

        # envoyer dans groupe admin
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
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=admin_preview,
                reply_markup=keyboard
            )

        # réponse à l'utilisateur
        await msg.reply_text("✅ Reçu. Merci. Vérif avant publication.")
        return

    # ----- CAS 2 : ALBUM -----
    album = TEMP_ALBUMS.get(media_group_id)

    if album is None:
        TEMP_ALBUMS[media_group_id] = {
            "files": [],
            "text": piece_text,
            "user_name": user_name,
            "ts": _now(),
            "chat_id": msg.chat_id,
        }
        album = TEMP_ALBUMS[media_group_id]

    # ajouter ce média dans l'album
    if media_type in ["photo", "video"]:
        album["files"].append({
            "type": media_type,
            "file_id": file_id,
        })

    # si du texte arrive et qu'on n'en avait pas encore stocké
    if piece_text and not album["text"]:
        album["text"] = piece_text

    album["ts"] = _now()

    # on déclenche la finalisation après un mini délai
    asyncio.create_task(
        finalize_album_later(media_group_id, context, msg)
    )


async def finalize_album_later(media_group_id, context: ContextTypes.DEFAULT_TYPE, original_msg):
    # On attend 0.5s pour laisser Telegram envoyer toutes les pièces du même album
    await asyncio.sleep(0.5)

    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        return

    report_id = f"{album['chat_id']}_{media_group_id}"

    PENDING[report_id] = {
        "files": album["files"],
        "text": album["text"],
    }

    # Aperçu pour admin
    admin_preview = f"📩 Nouveau signalement (album)\n👤 {album['user_name']}"
    if album["text"]:
        admin_preview += f"\n\n{album['text']}"

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
        ]]
    )

    files = album["files"]

    # 1 SEUL média dans l'album -> on envoie direct avec boutons
    if len(files) == 1:
        m = files[0]
        if m["type"] == "photo":
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=m["file_id"],
                caption=admin_preview,
                reply_markup=keyboard
            )
        else:
            await context.bot.send_video(
                chat_id=ADMIN_GROUP_ID,
                video=m["file_id"],
                caption=admin_preview,
                reply_markup=keyboard
            )
    else:
        # plusieurs médias -> on envoie le groupe puis un message bouton
        media_group = []
        for i, m in enumerate(files):
            if m["type"] == "photo":
                media_group.append(InputMediaPhoto(
                    media=m["file_id"],
                    caption=admin_preview if i == 0 else None
                ))
            else:
                media_group.append(InputMediaVideo(
                    media=m["file_id"],
                    caption=admin_preview if i == 0 else None
                ))

        # envoi de l'album (sans boutons)
        await context.bot.send_media_group(
            chat_id=ADMIN_GROUP_ID,
            media=media_group
        )

        # juste après, envoi du message texte + boutons
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_preview,
            reply_markup=keyboard
        )

    # nettoyer le cache temporaire de l'album
    TEMP_ALBUMS.pop(media_group_id, None)

    # répondre à l'utilisateur (évite spam si déjà répondu)
    try:
        await original_msg.reply_text("✅ Reçu (album). Vérif avant publication.")
    except Exception:
        pass


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # "APPROVE|report_id" ou "REJECT|report_id"
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
        caption_for_public = text if text else None

        # Cas : juste du texte
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

        # Cas : un seul média
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

            await safe_edit(query, "✅ Publié dans le groupe public.")
            PENDING.pop(report_id, None)
            return

        # Cas : plusieurs médias -> album public
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

        await safe_edit(query, "✅ Publié (album) dans le groupe public.")
        PENDING.pop(report_id, None)


async def safe_edit(query, new_text: str):
    # essaie d'éditer la légende du message bouton, sinon le texte
    try:
        await query.edit_message_caption(caption=new_text)
    except Exception:
        try:
            await query.edit_message_text(text=new_text)
        except Exception:
            pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # tout message au bot
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    # clic sur ✅ / ❌
    app.add_handler(CallbackQueryHandler(on_button_click))

    app.run_polling()


if __name__ == "__main__":
    main()
