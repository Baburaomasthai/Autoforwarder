import json
import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
    CommandHandler,
)
from telegram.error import TelegramError

# Setup logging
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

REPLACEMENTS_FILE = "replacements.json"
CONFIG_FILE = "config.json"

# Bot state
BOT_RUNNING = True

# Load JSON files
def load_json(file, default=None):
    if default is None:
        default = {"links": {}, "words": {}, "sentences": {}} if file == REPLACEMENTS_FILE else {"source_channels": [], "target_channel": "", "running": True}
    try:
        if not os.path.exists(file):
            with open(file, "w") as f:
                json.dump(default, f, indent=2)
        with open(file, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {file}: {e}")
        return default

def save_json(file, data):
    try:
        with open(file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {file}: {e}")

replacements = load_json(REPLACEMENTS_FILE)
config = load_json(CONFIG_FILE)

# Apply replacements to text
def apply_replacements(text):
    if not text:
        return text
    for old, new in replacements["links"].items():
        text = text.replace(old, new)
    for old, new in replacements["words"].items():
        text = text.replace(old, new)
    for old, new in replacements["sentences"].items():
        text = text.replace(old, new)
    return text

# Forward message handler
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_RUNNING
    if not BOT_RUNNING:
        return
    if not update.channel_post:
        return
    msg = update.channel_post
    if msg.chat.username not in config["source_channels"] and str(msg.chat.id) not in config["source_channels"]:
        return

    try:
       ಸ
        if msg.text or msg.caption:
            modified_text = apply_replacements(msg.text or msg.caption or "")
            await context.bot.send_message(chat_id=config["target_channel"], text=modified_text)
        elif msg.photo:
            await context.bot.send_photo(chat_id=config["target_channel"], photo=msg.photo[-1].file_id, caption=apply_replacements(msg.caption or ""))
        elif msg.video:
            await context.bot.send_video(chat_id=config["target_channel"], video=msg.video.file_id, caption=apply_replacements(msg.caption or ""))
        elif msg.document:
            await context.bot.send_document(chat_id=config["target_channel"], document=msg.document.file_id, caption=apply_replacements(msg.caption or ""))
        elif msg.sticker:
            await context.bot.send_sticker(chat_id=config["target_channel"], sticker=msg.sticker.file_id)
        elif msg.audio:
            await context.bot.send_audio(chat_id=config["target_channel"], audio=msg.audio.file_id, caption=apply_replacements(msg.caption or ""))
        elif msg.voice:
            await context.bot.send_voice(chat_id=config["target_channel"], voice=msg.voice.file_id, caption=apply_replacements(msg.caption or ""))
        elif msg.video_note:
            await context.bot.send_video_note(chat_id=config["target_channel"], video_note=msg.video_note.file_id)
        else:
            await msg.copy(chat_id=config["target_channel"])
    except TelegramError as e:
        logger.error(f"Error forwarding message: {e}")
        if str(e).lower().find("blocked by user") != -1 or str(e).lower().find("chat not found") != -1:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Error: Cannot send to target channel {config['target_channel']}. Check permissions or channel ID.")

# Check if user is admin
async def check_admin(update: Update):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized: You are not the admin.")
        return False
    return True

# Admin commands
async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_RUNNING
    if not await check_admin(update):
        return
    BOT_RUNNING = True
    config["running"] = True
    save_json(CONFIG_FILE, config)
    await update.message.reply_text("✅ Bot forwarding started.")

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_RUNNING
    if not await check_admin(update):
        return
    BOT_RUNNING = False
    config["running"] = False
    save_json(CONFIG_FILE, config)
    await update.message.reply_text("⛔ Bot forwarding stopped.")

async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        args = " ".join(context.args).split("=>")
        if len(args) != 2:
            raise ValueError
        old, new = args[0].strip(), args[1].strip()
        replacements["links"][old] = new
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Link replaced:\n{old} => {new}")
    except Exception:
        await update.message.reply_text("❌ Format: /addlink old => new")

async def add_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        args = " ".join(context.args).split("=>")
        if len(args) != 2:
            raise ValueError
        old, new = args[0].strip(), args[1].strip()
        replacements["words"][old] = new
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Word replaced:\n{old} => {new}")
    except Exception:
        await update.message.reply_text("❌ Format: /addword old => new")

async def add_sentence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        args = " ".join(context.args).split("=>")
        if len(args) != 2:
            raise ValueError
        old, new = args[0].strip(), args[1].strip()
        replacements["sentences"][old] = new
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Sentence replaced:\n{old} => {new}")
    except Exception:
        await update.message.reply_text("❌ Format: /addsentence old => new")

async def remove_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        link = " ".join(context.args).strip()
        if link in replacements["links"]:
            del replacements["links"][link]
            save_json(REPLACEMENTS_FILE, replacements)
            await update.message.reply_text(f"✅ Removed link: {link}")
        else:
            await update.message.reply_text("❌ Link not found.")
    except Exception:
        await update.message.reply_text("❌ Format: /removelink link")

async def remove_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        word = " ".join(context.args).strip()
        if word in replacements["words"]:
            del replacements["words"][word]
            save_json(REPLACEMENTS_FILE, replacements)
            await update.message.reply_text(f"✅ Removed word: {word}")
        else:
            await update.message.reply_text("❌ Word not found.")
    except Exception:
        await update.message.reply_text("❌ Format: /removeword word")

async def remove_sentence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        sentence = " ".join(context.args).strip()
        if sentence in replacements["sentences"]:
            del replacements["sentences"][sentence]
            save_json(REPLACEMENTS_FILE, replacements)
            await update.message.reply_text(f"✅ Removed sentence: {sentence}")
        else:
            await update.message.reply_text("❌ Sentence not found.")
    except Exception:
        await update.message.reply_text("❌ Format: /removesentence sentence")

async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        channel = context.args[0].strip()
        if channel not in config["source_channels"]:
            config["source_channels"].append(channel)
            save_json(CONFIG_FILE, config)
            await update.message.reply_text(f"✅ Added source channel: {channel}")
        else:
            await update.message.reply_text(f"❌ Channel {channel} already added.")
    except Exception:
        await update.message.reply_text("❌ Format: /addsource @channel")

async def remove_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        channel = context.args[0].strip()
        if channel in config["source_channels"]:
            config["source_channels"].remove(channel)
            save_json(CONFIG_FILE, config)
            await update.message.reply_text(f"✅ Removed source channel: {channel}")
        else:
            await update.message.reply_text(f"❌ Channel {channel} not found.")
    except Exception:
        await update.message.reply_text("❌ Format: /removesource @channel")

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        config["target_channel"] = context.args[0].strip()
        save_json(CONFIG_FILE, config)
        await update.message.reply_text(f"✅ Target channel set: {config['target_channel']}")
    except Exception:
        await update.message.reply_text("❌ Format: /setdst @channel")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    status = "running" if BOT_RUNNING else "stopped"
    await update.message.reply_text(
        f"📊 Bot Status:\n"
        f"Running: {status}\n"
        f"Source Channels: {', '.join(config['source_channels']) or 'None'}\n"
        f"Target Channel: {config['target_channel'] or 'None'}\n"
        f"Replacements: {len(replacements['links']) + len(replacements['words']) + len(replacements['sentences'])} active"
    )

async def show_replacements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    if not any(replacements.values()):
        await update.message.reply_text("📌 No replacements configured.")
        return
    await update.message.reply_text(
        f"📌 Replacements:\n```json\n{json.dumps(replacements, indent=2, ensure_ascii=False)}```",
        parse_mode="Markdown",
    )

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")
    if update and update.effective_user.id == ADMIN_ID:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Error: {context.error}")

# Main function
def main():
    if not BOT_TOKEN or not ADMIN_ID:
        logger.error("BOT_TOKEN or ADMIN_ID not set in .env file.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, forward_message))
    app.add_handler(CommandHandler("startbot", start_bot))
    app.add_handler(CommandHandler("stopbot", stop_bot))
    app.add_handler(CommandHandler("addlink", add_link))
    app.add_handler(CommandHandler("addword", add_word))
    app.add_handler(CommandHandler("addsentence", add_sentence))
    app.add_handler(Removed in the following line to avoid redundancy)
    app.add_handler(CommandHandler("removelink", remove_link))
    app.add_handler(CommandHandler("removeword", remove_word))
    app.add_handler(CommandHandler("removesentence", remove_sentence))
    app.add_handler(CommandHandler("addsource", add_source))
    app.add_handler(CommandHandler("removesource", remove_source))
    app.add_handler(CommandHandler("setdst", set_target))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("showreplacements", show_replacements))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
