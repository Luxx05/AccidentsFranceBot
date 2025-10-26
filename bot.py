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
BOT_TOKEN = os.getenv("BOT_TOKEN")  # défini dans Render
ADMIN_GROUP_ID = -1003294631521   # <-- MET ICI l'ID du groupe admin (commence par -100)
PUBLIC_GROUP_ID = -1003245719893    # groupe public
# =====================

# mémoire temporaire des signalements en attente
# {unique_id: {"from_chat":..., "message_id":..., "type":..., "file_id":..., "user_text":...}}
PENDING = {}


# /start -> message d'accueil utilisateur
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenue sur ACCIDENTS FRANCE 🚨\n\n"
        "Envoie ici ta vidéo / photo / description d'accident.\n"
        "On vérifie avant de publier.\n"
        "Tu restes anonyme."
    )


# réception d'un message utilisateur (photo / vidéo / texte)
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    # texte utilisateur (peut être vide)
    user_text = msg.text.strip() if msg.text else ""

    # détecter type et récupérer file_id si média
    if msg.video:
        content_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        content_type = "photo"
        file_id = msg.photo[-1].file_id
    else:
        content_type = "text"
        file_id = None

    # créer un ID unique pour ce signalement
    unique_id = f"{msg.chat_id}_{msg.id}"

    # on stocke toutes les infos pour plus tard (quand tu cliques ✅)
    PENDING[unique_id] = {
        "from_chat": msg.chat_id,
        "message_id": msg.id,
        "type": content_type,
        "file_id": file_id,
        "user_text": user_text,
    }

    # message que TU vois dans le groupe admin (avec pseudo pour ton tri)
    preview_lines = []
    preview_lines.append("📥 Nouveau signalement")
    preview_lines.append(f"👤 De @{user.username or 'anonyme'}")
    if user_text:
        preview_lines.append(f"📝 {user_text}")

    preview_caption = "\n".join(preview_lines)

    # clavier pour toi dans le groupe admin
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
            caption=preview_caption,
            reply_markup=keyboard
        )
    elif content_type == "photo":
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=file_id,
            caption=preview_caption,
            reply_markup=keyboard
        )
    else:
        # texte seul
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=preview_caption,
            reply_markup=keyboard
        )

    # réponse auto à l'utilisateur
    await msg.reply_text("✅ Reçu. Merci. Publication après vérif.")


# clic sur bouton ✅ / ❌ dans le groupe admin
async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # "APPROVE|..." ou "REJECT|..."
    action, unique_id = data.split("|", 1)

    info = PENDING.get(unique_id)
    if not info:
        # déjà traité ou le bot a redémarré
        await safe_edit(query, "⛔ Déjà traité / introuvable.")
        return

    # ❌ Refus
    if action == "REJECT":
        await safe_edit(
            query,
            "❌ Refusé. Non publié."
        )
        PENDING.pop(unique_id, None)
        return

    # ✅ Approve
    if action == "APPROVE":
        media_type = info["type"]
        file_id = info["file_id"]
        user_text = info["user_text"].strip() if info["user_text"] else ""

        # LÉGENDE FINALE POUR LE PUBLIC :
        # -> si la personne a écrit du texte, on publie ce texte
        # -> sinon rien (juste la vidéo/photo sans légende)
        final_caption = user_text if user_text else None

        # Envoi public
        if media_type == "video":
            await context.bot.send_video(
                chat_id=PUBLIC_GROUP_ID,
                video=file_id,
                caption=final_caption  # peut être None
            )
        elif media_type == "photo":
            await context.bot.send_photo(
                chat_id=PUBLIC_GROUP_ID,
                photo=file_id,
                caption=final_caption  # peut être None
            )
        else:
            # texte seul
            if final_caption:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID,
                    text=final_caption
                )
            else:
                # cas extrême : pas de média + pas de texte = on ne poste rien
                await safe_edit(query, "❌ Rien à publier (vide).")
                PENDING.pop(unique_id, None)
                return

        # update du message dans le groupe admin
        await safe_edit(
            query,
            "✅ Publié dans le groupe public."
        )

        # on enlève de la mémoire
        PENDING.pop(unique_id, None)


async def safe_edit(query, new_text: str):
    """
    Essaie d'éditer soit la caption (si média),
    soit le texte du message admin.
    Évite l'erreur 'There is no text in the message to edit'.
    """
    try:
        # si c'était une photo/vidéo avec légende
        await query.edit_message_caption(caption=new_text)
    except:
        # si c'était un message texte
        try:
            await query.edit_message_text(text=new_text)
        except:
            # on ignore si Telegram refuse encore
            pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", start))

    # messages utilisateurs (photo, vidéo, texte sans /command)
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | (filters.TEXT & ~filters.COMMAND),
        handle_user_message
    ))

    # boutons d'approbation
    app.add_handler(CallbackQueryHandler(on_button_click))

    # run bot
    app.run_polling()


if __name__ == "__main__":
    main()
