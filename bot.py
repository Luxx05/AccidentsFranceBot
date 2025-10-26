import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN")  # défini dans Render
ADMIN_GROUP_ID = -5694113795        # ton groupe privé admin
PUBLIC_GROUP_ID = -1003245719893    # ton groupe public
# =====================

# on garde en mémoire les messages en attente
PENDING = {}  # {unique_id: {"file_id":..., "type":..., "caption":...}}

# /start -> message d’accueil pour l’utilisateur
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenue sur ACCIDENTS FRANCE 🚨\n\n"
        "Envoie ici ta vidéo / photo / info d'accident.\n"
        "On vérifie avant de publier dans le groupe public.\n\n"
        "Tu restes anonyme, t'inquiète."
    )

# quand quelqu’un envoie un message (photo / vidéo / texte)
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    # légende de base
    caption = f"Signalement reçu.\nDe @{user.username or 'anonyme'}"
    if msg.text:
        caption += f"\nTexte: {msg.text}"

    # id unique pour ce signalement
    unique_id = f"{msg.chat_id}_{msg.id}"

    # détecter le type de contenu + récupérer l'ID du media
    if msg.video:
        content_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        content_type = "photo"
        file_id = msg.photo[-1].file_id  # meilleure résolution
    else:
        content_type = "text"
        file_id = None

    # stocker en attente
    PENDING[unique_id] = {
        "from_chat": msg.chat_id,
        "message_id": msg.id,
        "type": content_type,
        "file_id": file_id,
        "caption": caption,
    }

    # boutons pour toi dans le groupe admin
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{unique_id}"),
            InlineKeyboardButton("❌ Refuser", callback_data=f"REJECT|{unique_id}")
        ]]
    )

    # envoyer dans le groupe admin pour validation
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
        # juste du texte
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=caption,
            reply_markup=keyboard
        )

    # réponse à l'utilisateur
    await msg.reply_text("✅ Reçu. Merci. On vérifie avant de poster.")


# quand TU cliques sur ✅ Publier ou ❌ Refuser
async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # exemple "APPROVE|12345_6789"
    action, unique_id = data.split("|", 1)

    # récupérer le signalement stocké
    info = PENDING.get(unique_id)
    if not info:
        # déjà traité / redémarré
        await query.edit_message_text("⛔ Déjà traité.")
        return

    if action == "REJECT":
        # marquer comme refusé et enlever de la file
        await query.edit_message_text(info["caption"] + "\n\n❌ Refusé. Non publié.")
        PENDING.pop(unique_id, None)
        return

    if action == "APPROVE":
        # publier dans le groupe public
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

        # confirmer côté admin
        await query.edit_message_text(info["caption"] + "\n\n✅ Publié dans le groupe public.")
        # supprimer de la mémoire
        PENDING.pop(unique_id, None)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", start))

    # messages utilisateurs (photo / vidéo / texte)
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.TEXT & ~filters.COMMAND,
        handle_user_message
    ))

    # boutons ✅ ❌
    app.add_handler(CallbackQueryHandler(on_button_click))

    # run
    app.run_polling()


if __name__ == "__main__":
    main()



