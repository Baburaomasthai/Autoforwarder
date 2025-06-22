import json
import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
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

# Load JSON with default fallback
def load_json(file, default=None):
    if default is None:
        default = {
            "links": {}, "words": {}, "sentences": {}
        } if file == REPLACEMENTS_FILE else {
            "source_channels": [], "target_channel": "", "running": True
        }
    try:
        if not os.path.exists(file):
            with open(file, "w") as f:
                json.dump(default, f, indent=2)
        with open(file, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {file}: {e}")
        return default

# Save JSON data
def save_json(file, data):
    try:
        with open(file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {file}: {e}")

# Load configs
replacements = load_json(REPLACEMENTS_FILE)
config = load_json(CONFIG_FILE)

# Check admin
async def check_admin(update: Update):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized: You are not the admin.")
        return False
    return True

# Commands
async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    config["running"] = True
    save_json(CONFIG_FILE, config)
    await update.message.reply_text("✅ Bot forwarding started.")

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    config["running"] = False
    save_json(CONFIG_FILE, config)
    await update.message.reply_text("⛔ Bot forwarding stopped.")

async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
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

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        channel = context.args[0].strip()
        config["target_channel"] = channel
        save_json(CONFIG_FILE, config)
        await update.message.reply_text(f"✅ Target channel set to: {channel}")
    except Exception:
        await update.message.reply_text("❌ Format: /setdst @channel")

async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        args = " ".join(context.args).split("=>")
        if len(args) != 2:
            raise ValueError
        old, new = args[0].strip(), args[1].strip()
        replacements["links"][old] = new
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Link replacement added:\n{old} => {new}")
    except Exception:
        await update.message.reply_text("❌ Format: /addlink old => new")

def main():
    if not BOT_TOKEN or not ADMIN_ID:
        logger.error("❌ BOT_TOKEN or ADMIN_ID not set in .env file.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("startbot", start_bot))
    app.add_handler(CommandHandler("stopbot", stop_bot))
    app.add_handler(CommandHandler("addsource", add_source))
    app.add_handler(CommandHandler("setdst", set_target))
    app.add_handler(CommandHandler("addlink", add_link))

    logger.info("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()