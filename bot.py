import os
import asyncio
import logging
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1011608275"))
STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL", "-1003669782876"))

# In-memory store for batch index: {batch_id: [msg_ids]}
batch_index = {}

# Temp storage for media groups
media_group_cache = defaultdict(list)
media_group_timers = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    params = context.args

    if not params:
        keyboard = [[InlineKeyboardButton("📤 Upload Files", callback_data="upload")]]
        await update.message.reply_text(
            "<b>📚 Welcome to LullBot!</b>\n\n"
            "• Upload photos, videos, documents\n"
            "• Get a permanent shareable link\n"
            "• Share with classmates — files never expire\n\n"
            "<b>How to use:</b>\n"
            "1. Tap 📤 Upload Files\n"
            "2. Send your files one by one or as album\n"
            "3. Type /done when finished\n"
            "4. Get your permanent link!",
            parse_mode="html",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        batch_id = params[0]
        await deliver_files(update, context, batch_id)


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only the admin can upload files.")
        return

    context.user_data["uploading"] = True
    context.user_data["collected_files"] = []

    await update.message.reply_text(
        "📤 <b>Upload session started!</b>\n\n"
        "Send your files now (photos, videos, docs).\n"
        "Albums work too.\n\n"
        "Type /done when finished.",
        parse_mode="html"
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get("uploading"):
        await update.message.reply_text("Type /upload to start a session first.")
        return

    message = update.message
    media_group_id = message.media_group_id

    if media_group_id:
        media_group_cache[media_group_id].append(message)

        if media_group_id in media_group_timers:
            media_group_timers[media_group_id].cancel()

        loop = asyncio.get_event_loop()
        timer = loop.call_later(
            1.5,
            lambda mgid=media_group_id: asyncio.ensure_future(
                process_media_group(mgid, context, update)
            )
        )
        media_group_timers[media_group_id] = timer
    else:
        if "collected_files" not in context.user_data:
            context.user_data["collected_files"] = []

        file_info = extract_file_info(message)
        if file_info:
            context.user_data["collected_files"].append(file_info)
            count = len(context.user_data["collected_files"])
            await message.reply_text(f"✅ File {count} received. Send more or /done.")


async def process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    messages = media_group_cache.pop(media_group_id, [])
    media_group_timers.pop(media_group_id, None)

    if not messages:
        return

    if "collected_files" not in context.user_data:
        context.user_data["collected_files"] = []

    count_before = len(context.user_data["collected_files"])

    for msg in messages:
        file_info = extract_file_info(msg)
        if file_info:
            context.user_data["collected_files"].append(file_info)

    added = len(context.user_data["collected_files"]) - count_before
    total = len(context.user_data["collected_files"])

    try:
        await messages[0].reply_text(
            f"✅ Album received ({added} files). Total so far: {total}. Send more or /done."
        )
    except Exception as e:
        logger.error(f"Album confirmation error: {e}")


def extract_file_info(message):
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "caption": message.caption or ""}
    elif message.video:
        return {"type": "video", "file_id": message.video.file_id, "caption": message.caption or ""}
    elif message.document:
        return {"type": "document", "file_id": message.document.file_id, "caption": message.caption or ""}
    elif message.audio:
        return {"type": "audio", "file_id": message.audio.file_id}
    elif message.voice:
        return {"type": "voice", "file_id": message.voice.file_id}
    elif message.animation:
        return {"type": "animation", "file_id": message.animation.file_id}
    return None


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get("uploading"):
        await update.message.reply_text("No active session. Type /upload first.")
        return

    await asyncio.sleep(2)  # wait for any pending album timer

    files = context.user_data.get("collected_files", [])

    if not files:
        await update.message.reply_text("❌ No files uploaded. Send files first.")
        return

    await update.message.reply_text(f"⏳ Saving {len(files)} file(s) permanently...")

    try:
        msg_ids = []
        for f in files:
            sent = await send_file_to_channel(context.bot, f)
            if sent:
                msg_ids.append(sent.message_id)

        if not msg_ids:
            await update.message.reply_text("❌ Failed to save. Try again.")
            return

        batch_id = str(msg_ids[0])

        # Save index to channel as a text message
        ids_str = ",".join(map(str, msg_ids))
        await context.bot.send_message(
            STORAGE_CHANNEL,
            f"BATCH:{batch_id}:{ids_str}"
        )

        # Also keep in memory for this session
        batch_index[batch_id] = msg_ids

        bot_username = (await context.bot.get_me()).username
        shareable_link = f"https://t.me/{bot_username}?start={batch_id}"

        context.user_data["uploading"] = False
        context.user_data["collected_files"] = []

        keyboard = [[InlineKeyboardButton("📤 Upload More", callback_data="upload")]]

        await update.message.reply_text(
            f"✅ <b>{len(files)} file(s) saved permanently!</b>\n\n"
            f"🔗 Shareable Link:\n<code>{shareable_link}</code>\n\n"
            f"This link never expires. Copy and share it.",
            parse_mode="html",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"done_command error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def send_file_to_channel(bot, file_info):
    try:
        if file_info["type"] == "photo":
            return await bot.send_photo(STORAGE_CHANNEL, file_info["file_id"], caption=file_info.get("caption", ""))
        elif file_info["type"] == "video":
            return await bot.send_video(STORAGE_CHANNEL, file_info["file_id"], caption=file_info.get("caption", ""))
        elif file_info["type"] == "document":
            return await bot.send_document(STORAGE_CHANNEL, file_info["file_id"], caption=file_info.get("caption", ""))
        elif file_info["type"] == "audio":
            return await bot.send_audio(STORAGE_CHANNEL, file_info["file_id"])
        elif file_info["type"] == "voice":
            return await bot.send_voice(STORAGE_CHANNEL, file_info["file_id"])
        elif file_info["type"] == "animation":
            return await bot.send_animation(STORAGE_CHANNEL, file_info["file_id"])
    except Exception as e:
        logger.error(f"send_file_to_channel error: {e}")
        return None


async def deliver_files(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str):
    await update.message.reply_text("⏳ Fetching your files...")

    try:
        # Check in-memory index first
        msg_ids = batch_index.get(batch_id)

        # If not in memory (bot restarted), search channel messages
        if not msg_ids:
            target_text = f"BATCH:{batch_id}:"
            # Search last 1000 messages in channel
            found = False
            offset = 0
            while not found:
                updates = await context.bot.get_updates(offset=offset, limit=100)
                if not updates:
                    break
                for u in updates:
                    if (u.channel_post and
                        u.channel_post.chat.id == STORAGE_CHANNEL and
                        u.channel_post.text and
                        u.channel_post.text.startswith(target_text)):
                        parts = u.channel_post.text.split(":")
                        msg_ids = list(map(int, parts[2].split(",")))
                        batch_index[batch_id] = msg_ids
                        found = True
                        break
                offset += len(updates)
                if len(updates) < 100:
                    break

        if not msg_ids:
            await update.message.reply_text(
                "❌ Files not found.\n\n"
                "Note: If the bot was recently restarted, index may be lost. "
                "Contact admin to re-upload."
            )
            return

        for msg_id in msg_ids:
            try:
                await context.bot.forward_message(
                    chat_id=update.effective_chat.id,
                    from_chat_id=STORAGE_CHANNEL,
                    message_id=msg_id
                )
                await asyncio.sleep(0.3)  # avoid flood limits
            except Exception as e:
                logger.error(f"Forward error for msg {msg_id}: {e}")

        await update.message.reply_text("✅ All files delivered!")

    except Exception as e:
        logger.error(f"deliver_files error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "upload":
        if query.from_user.id != ADMIN_ID:
            await query.message.reply_text("❌ Only admin can upload.")
            return
        context.user_data["uploading"] = True
        context.user_data["collected_files"] = []
        await query.message.reply_text(
            "📤 <b>Upload session started!</b>\n\nSend your files. Type /done when finished.",
            parse_mode="html"
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upload", upload_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL |
        filters.AUDIO | filters.VOICE | filters.ANIMATION,
        handle_media
    ))

    logger.info("LullBot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
