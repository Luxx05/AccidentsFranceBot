import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN")  # variable d'env Render
ADMIN_GROUP_ID = -5694113795        # groupe privé admin (toi)
PUBLIC_GROUP_ID = -1003245719893    # groupe public
# =====================

PENDING = {}  # stocke les signalements en attente


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenue sur ACCIDENTS FRANCE 🚨\n\n"
        "Envoie une vidéo / photo / info d'accident ici.\n"
        "On vérifie avant de publier dans le groupe public.\n"
        "Tu restes anonyme."
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    caption = f"Signalement reçu.\nDe @{user.username or 'anonyme'}"
    if msg.text:
        caption += f"\nTexte: {msg.text}"

    unique_id = f"{msg.chat_id}_{msg.id}"

    if msg.video:
        content_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        content_type = "photo"
        file_id = msg.photo[-1].file_id
    else:
        content_type = "text"
        file_id = None

    PENDING[unique_id] = {
        "from_chat": msg.chat_id,
        "message_id": msg.id,
        "type": content_type,
        "file_id": file_id,
        "caption": caption,
    }

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{unique_id}"),
            InlineKeyboardButton("❌ Refuser", callback_data=f"REJECT|{unique_id}")
        ]]
    )

    # Envoi pour validation dans le groupe admin
    if content_type == "video":
        await context.bot.send_video(
            chat_id=ADMIN_GROUP_ID,
            video=file_id,
            caption=caption,
            reply_markup=keyboard
        )
    elif content_type == "photo":
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=file_id,
            caption=caption,
            reply_markup=keyboard
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=caption,
            reply_markup=keyboard
        )

    # Retour utilisateur
    await msg.reply_text("✅ Reçu. Merci. Vérif en cours.")


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # "APPROVE|... " ou "REJECT|..."
    action, unique_id = data.split("|", 1)

    info = PENDING.get(unique_id)
    if not info:
        await query.edit_message_text("⛔ Déjà traité.")
        return

    # ❌ Refusé
    if action == "REJECT":
        await query.edit_message_text(
            info["caption"] + "\n\n❌ Refusé. Non publié."
        )
        PENDING.pop(unique_id, None)
        return

    # ✅ Approuvé
    if action == "APPROVE":
        if info["type"] == "video":
            await context.bot.send_video(
                chat_id=PUBLIC_GROUP_ID,
                video=info["file_id"],
                caption=info["caption"] + "\n\n#signalement"
            )
        elif info["type"] == "photo":
            await context.bot.send_photo(
                chat_id=PUBLIC_GROUP_ID,
                photo=info["file_id"],
                caption=info["caption"] + "\n\n#signalement"
            )
        else:
            await context.bot.send_message(
                chat_id=PUBLIC_GROUP_ID,
                text=info["caption"] + "\n\n#signalement"
            )

        await query.edit_message_text(
            info["caption"] + "\n\n✅ Publié dans le groupe public."
        )

        PENDING.pop(unique_id, None)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", start))

    # messages (photo, vidéo, texte sans commande)
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | (filters.TEXT & ~filters.COMMAND),
        handle_user_message
    ))

    # boutons ✅ / ❌
    app.add_handler(CallbackQueryHandler(on_button_click))

    app.run_polling()


if __name__ == "__main__":
    main()
