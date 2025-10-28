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

# =========== CONFIG ===========
# IMPORTANT: vérifie ces 3 valeurs pour qu'elles matchent ton setup

BOT_TOKEN = os.getenv("BOT_TOKEN")  # sur Render tu as déjà mis la variable d'env BOT_TOKEN

ADMIN_GROUP_ID = -1003294631521    # groupe privé admin (là où tu reçois les signalements pour valider)
PUBLIC_GROUP_ID = -1003245719893   # groupe public (là où ça publie quand tu approuves)

# IDs topics dans le groupe PUBLIC
PUBLIC_TOPIC_VIDEOS_ID = 224       # topic "🎥 Vidéos & Dashcams"
PUBLIC_TOPIC_RADARS_ID = 222       # topic "📍 Radars & Signalements"

# Dictionnaire des signalements en attente
# PENDING[report_id] = {
#   "files": [ {"type": "photo"/"video", "file_id": "..."} ],
#   "text": "....",
#   "user_id": <telegram user id>   # pour pouvoir DM après approbation
# }
PENDING = {}

# TEMP_ALBUMS : album en cours de réception chez le bot
# TEMP_ALBUMS[media_group_id] = {
#   "files": [...],
#   "text": "...",
#   "user_name": "...",
#   "ts": timestamp_last_piece,
#   "chat_id": chat_id,
#   "done": False
# }
TEMP_ALBUMS = {}
# =============================


def _now():
    return time.time()


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reçoit les messages envoyés DIRECTEMENT au bot (DM bot).
    Stocke en attente + envoie aperçu dans le groupe admin avec boutons.
    """
    msg = update.message
    user = msg.from_user

    piece_text = (msg.caption or msg.text or "").strip()
    user_name = f"@{user.username}" if user.username else "anonyme"

    # Détection média
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

    # ----- CAS 1 : message simple (pas album) -----
    if media_group_id is None:
        report_id = f"{msg.chat_id}_{msg.id}"

        # on crée l'entrée en attente
        PENDING[report_id] = {
            "files": [],
            "text": piece_text,
            "user_id": user.id,  # on garde qui a envoyé -> pour DM après approbation
        }

        if media_type in ["photo", "video"]:
            PENDING[report_id]["files"].append({
                "type": media_type,
                "file_id": file_id,
            })

        # preview à envoyer dans le groupe admin
        admin_preview = f"📩 Nouveau signalement\n👤 {user_name}"
        if piece_text:
            admin_preview += f"\n\n{piece_text}"

        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Publier", callback_data=f"APPROVE|{report_id}"),
                InlineKeyboardButton("❌ Supprimer", callback_data=f"REJECT|{report_id}")
            ]]
        )

        # envoi dans le groupe admin (sans routing par topic admin pour rester simple)
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

        # feedback user
        await msg.reply_text("✅ Reçu. Vérif avant publication.")
        return

    # ----- CAS 2 : album (plusieurs médias envoyés en une fois) -----
    album = TEMP_ALBUMS.get(media_group_id)

    if album is None:
        TEMP_ALBUMS[media_group_id] = {
            "files": [],
            "text": piece_text,
            "user_name": user_name,
            "ts": _now(),
            "chat_id": msg.chat_id,  # on garde pour DM plus tard
            "done": False,
        }
        album = TEMP_ALBUMS[media_group_id]

    # on ajoute la pièce actuelle
    if media_type in ["photo", "video"]:
        album["files"].append({
            "type": media_type,
            "file_id": file_id,
        })

    # si texte fourni dans cette pièce et pas encore stocké
    if piece_text and not album["text"]:
        album["text"] = piece_text

    album["ts"] = _now()

    # on lance la finalisation (petit délai pour regrouper l'album)
    asyncio.create_task(
        finalize_album_later(media_group_id, context, msg)
    )


async def finalize_album_later(media_group_id, context: ContextTypes.DEFAULT_TYPE, original_msg):
    """
    Attend un mini délai pour recevoir tout l'album, puis push vers groupe admin.
    """
    await asyncio.sleep(0.5)

    album = TEMP_ALBUMS.get(media_group_id)
    if album is None:
        return

    if album.get("done"):
        return
    album["done"] = True

    report_id = f"{album['chat_id']}_{media_group_id}"

    # on met l'album en attente pour approbation
    PENDING[report_id] = {
        "files": album["files"],
        "text": album["text"],
        "user_id": album["chat_id"],  # pour pouvoir DM l'auteur après
    }

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

    # on envoie d'abord un message texte dans admin (avec boutons)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_preview,
            reply_markup=keyboard
        )
    except Exception:
        pass

    # puis on envoie les médias reçus pour contexte visuel (sans boutons)
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
        except Exception:
            pass
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
        except Exception:
            pass

    # on nettoie l'album en RAM
    TEMP_ALBUMS.pop(media_group_id, None)

    # feedback à l'utilisateur qui a envoyé l'album
    try:
        await original_msg.reply_text("✅ Reçu (album). Vérif avant publication.")
    except Exception:
        pass


async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Gère les clics sur ✅ Publier / ❌ Supprimer dans le groupe admin.
    Publie dans le bon topic du groupe public.
    Préviens l'utilisateur d'origine en DM.
    Supprime les boutons après.
    """
    query = update.callback_query
    await query.answer()

    data = query.data  # format "APPROVE|<report_id>" ou "REJECT|<report_id>"
    action, report_id = data.split("|", 1)

    info = PENDING.get(report_id)

    # si on n'a plus les infos (déjà traité)
    if not info:
        try:
            await query.edit_message_text("🚫 Déjà traité / introuvable.")
        except Exception:
            pass
        return

    # ---------- REJECT ----------
    if action == "REJECT":
        # feedback visuel : on enlève les boutons
        try:
            await query.edit_message_text("❌ Supprimé, non publié.")
        except Exception:
            pass

        # cleanup
        PENDING.pop(report_id, None)
        return

    # ---------- APPROVE ----------
    if action == "APPROVE":
        files = info["files"]
        text = (info["text"] or "").strip()
        caption_for_public = text if text else None
        original_user_id = info.get("user_id")  # pour DM après

        # choix du topic public
        text_lower = text.lower() if text else ""
        radar_keywords = ["radar", "radar mobile", "radar fixe", "contrôle", "controle", "laser", "flash", "police", "banalisée"]

        if any(keyword in text_lower for keyword in radar_keywords):
            target_thread_id = PUBLIC_TOPIC_RADARS_ID
        else:
            target_thread_id = PUBLIC_TOPIC_VIDEOS_ID

        # === CAS A : pas de médias -> texte seul ===
        if not files:
            if text:
                await context.bot.send_message(
                    chat_id=PUBLIC_GROUP_ID,
                    text=text,
                    message_thread_id=target_thread_id
                )

                # message privé user
                if original_user_id:
                    try:
                        await context.bot.send_message(
                            chat_id=original_user_id,
                            text="✅ Ton signalement a été publié."
                        )
                    except Exception as e:
                        print(f"[NOTIF USER TEXT] {e}")

                # feedback admin (enlève les boutons)
                try:
                    await query.edit_message_text("✅ Publié (texte).")
                except Exception:
                    pass
            else:
                try:
                    await query.edit_message_text("❌ Rien à publier (vide).")
                except Exception:
                    pass

            PENDING.pop(report_id, None)
            return

        # === CAS B : un seul média ===
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

            # DM à l'utilisateur
            if original_user_id:
                try:
                    await context.bot.send_message(
                        chat_id=original_user_id,
                        text="✅ Ta vidéo/photo a été publiée."
                    )
                except Exception as e:
                    print(f"[NOTIF USER MEDIA] {e}")

            # feedback admin
            try:
                await query.edit_message_text("✅ Publié (1 média).")
            except Exception:
                pass

            PENDING.pop(report_id, None)
            return

        # === CAS C : plusieurs médias (album) ===
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

        # envoi album dans le bon topic
        await context.bot.send_media_group(
            chat_id=PUBLIC_GROUP_ID,
            media=media_group,
            message_thread_id=target_thread_id
        )

        # DM user
        if original_user_id:
            try:
                await context.bot.send_message(
                    chat_id=original_user_id,
                    text="✅ Tes médias ont été publiés."
                )
            except Exception as e:
                print(f"[NOTIF USER ALBUM] {e}")

        # feedback admin (supprime les boutons)
        try:
            await query.edit_message_text("✅ Publié (album).")
        except Exception:
            pass

        PENDING.pop(report_id, None)
        return


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # tous les messages envoyés en privé au bot
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    # clic sur les boutons ✅ / ❌ dans le groupe admin
    app.add_handler(CallbackQueryHandler(on_button_click))

    # run poll
    app.run_polling(poll_interval=2.0)


# keep-alive loop (pour Render free)
import time as _t
while True:
    _t.sleep(60)


if __name__ == "__main__":
    main()
