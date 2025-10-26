from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes

# ========= CONFIG =========
BOT_TOKEN = "8143489143:AAHJvOY3FvTBb2Xsk30UZJGN_wW1YrHwvs0"  # token BotFather
ADMIN_CHAT_ID = -5694113795        # ton groupe admin privé (met un - devant l’ID)
PUBLIC_CHAT_ID = -1003245719893    # ton groupe public (ajoute toujours le -100 devant)
# ==========================

PENDING = {}

# /start - message d'accueil
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenue sur ACCIDENTS FRANCE 🚨\n\n"
        "📸 Envoie ici ton signalement (photo, vidéo, texte).\n"
        "Ton message sera vérifié avant publication sur le groupe public."
    )

# Réception utilisateur
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    caption = f"De @{user.username or 'un membre'}"
    if msg.text:
        caption += f"\nTexte: {msg.text}"

    unique_id = f"{msg.chat_id}_{msg.id}"
    content_type = "video" if msg.video else "photo" if msg.photo else "text"

    PENDING[unique_id] = {
        "from_chat": msg.chat_id,
        "message_id": msg.id,
        "caption": caption,
        "type": content_type
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{unique_id}"),
            InlineKeyboardButton("❌ Refuser", callback_data=f"REJECT|{unique_id}")
        ]
    ])

    if msg.video:
        await context.bot.send_video(ADMIN_CHAT_ID, msg.video.file_id, caption=caption, reply_markup=keyboard)
    elif msg.photo:
        await context.bot.send_photo(ADMIN_CHAT_ID, msg.photo[-1].file_id, caption=caption, reply_markup=keyboard)
    else:
        await context.bot.send_message(ADMIN_CHAT_ID, caption, reply_markup=keyboard)

    await msg.reply_text("✅ Signalement reçu, merci ! En attente de vérification.")

# Boutons admin
async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, unique_id = query.data.split("|", 1)
    info = PENDING.get(unique_id)
    if not info:
        await query.edit_message_caption(caption="(Déjà traité ou introuvable)")
        return

    if action == "REJECT":
        await query.edit_message_caption(caption=info["caption"] + "\n\n❌ Refusé.")
        PENDING.pop(unique_id, None)
        return

    if action == "APPROVE":
        typ = info["type"]
        if typ in ["video", "photo"]:
            await context.bot.copy_message(PUBLIC_CHAT_ID, info["from_chat"], info["message_id"],
                                           caption=info["caption"] + "\n\n#signalement")
        else:
            await context.bot.send_message(PUBLIC_CHAT_ID, info["caption"] + "\n\n#signalement")

        await query.edit_message_caption(caption=info["caption"] + "\n\n✅ Publié.")
        PENDING.pop(unique_id, None)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_user_message))
    app.add_handler(CallbackQueryHandler(on_button_click))
    app.run_polling()

if __name__ == "__main__":
    main()
