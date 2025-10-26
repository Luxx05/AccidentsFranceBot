import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = -1003294631521   # Groupe privé admin (où tu reçois les signalements)
PUBLIC_GROUP_ID = -1003245719893  # Groupe public (où tu publies après validation)
# =====================

# Mémoire temporaire
# {unique_id: {"type":..., "file_id":..., "text":...}}
PENDING = {}

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    # Récup texte possible (caption d’un média + texte brut)
    # ex: ils envoient une vidéo avec "accident A9", puis un msg "camion couché sens Espagne"
    # -> on fusionne les deux
    caption_text = msg.caption or ""
    normal_text = msg.text or ""
    combined_text = (caption_text + "\n" + normal_text).strip()

    # si vraiment rien du tout
    if combined_text == "":
        combined_text = "(aucune description)"

    user_name = f"@{user.username}" if user.username else "anonyme"

    # Type de contenu et file_id
    if msg.video:
        content_type = "video"
        file_id = msg.video.file_id
    elif msg.photo:
        content_type = "photo"
        file_id = msg.photo[-1].file_id
    else:
        content_type = "text"
        file_id = None

    unique_id = f"{msg.chat_id}_{msg.id}"

    # on stocke pour le bouton ✅/❌
    PENDING[unique_id] = {
        "type": content_type,
        "file_id": file_id,
        "text": combined_text,  # <= tjs le texte fusionné
    }

    # preview pour le groupe admin
    admin_preview = f"📩 Nouveau signalement\n👤 {user_name}\n\n{combined_text}"

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{unique_id}"),
            InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{unique_id}")
        ]]
    )

    # on envoie dans le groupe admin avec boutons
    if content_type == "video":
        await context.bot.send_video(
            chat_id=ADMIN_GROUP_ID,
            video=file_id,
            caption=admin_preview,
            reply_markup=keyboard
        )
    elif content_type == "photo":
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

    # réponse user
    await msg.reply_text("✅ Reçu. Merci. Vérif avant publication.")


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, unique_id = query.data.split("|", 1)
    info = PENDING.get(unique_id)

    if not info:
        await safe_edit(query, "⛔ Déjà traité / introuvable.")
        return

    if action == "REJECT":
        await safe_edit(query, "❌ Supprimé. Non publié.")
        PENDING.pop(unique_id, None)
        return

    if action == "APPROVE":
        content_type = info["type"]
        file_id = info["file_id"]
        final_text = info["text"].strip() if info["text"] else None

        # On publie dans le groupe public AVEC le texte fusionné
        if content_type == "video":
            await context.bot.send_video(
                chat_id=PUBLIC_GROUP_ID,
                video=file_id,
                caption=final_text if final_text else None
            )
        elif content_type == "photo":
            await context.bot.send_photo(
                chat_id=PUBLIC_GROUP_ID,
                photo=file_id,
                caption=final_text if final_text else None
            )
        else:
            if final_text:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID,
                    text=final_text
                )
            else:
                # rien à publier = on arrête
                await safe_edit(query, "❌ Rien à publier (vide).")
                PENDING.pop(unique_id, None)
                return

        await safe_edit(query, "✅ Publié dans le groupe public.")
        PENDING.pop(unique_id, None)


async def safe_edit(query, new_text: str):
    # essaie d'edit comme légende (photo/vidéo), sinon comme texte
    try:
        await query.edit_message_caption(caption=new_text)
    except:
        try:
            await query.edit_message_text(text=new_text)
        except:
            pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # tout message envoyé au bot (photo / vidéo / texte)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    # clic sur ✅ / ❌ dans le groupe admin
    app.add_handler(CallbackQueryHandler(on_button_click))

    app.run_polling()


if __name__ == "__main__":
    main()
