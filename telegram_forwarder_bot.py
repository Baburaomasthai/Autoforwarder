import json
import logging
import re
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration file
CONFIG_FILE = 'bot_config.json'

# Default configuration
DEFAULT_CONFIG = {
    'admin_id': None,  # Replace with your Telegram user ID
    'source_channels': [],  # List of source channel IDs or usernames
    'target_channel': None,  # Target channel ID or username
    'replacements': {},  # Dictionary of old -> new replacements
    'forwarding_active': False
}

# Load or initialize configuration
def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

config = load_config()

# Initialize bot
BOT_TOKEN = 'YOUR_BOT_TOKEN'  # Replace with your bot token
bot = Bot(token=BOT_TOKEN)

# Helper function to resolve channel username to chat ID
async def get_chat_id(channel: str) -> int:
    try:
        chat = await bot.get_chat(channel)
        return chat.id
    except TelegramError as e:
        logger.error(f"Error resolving channel {channel}: {e}")
        return None

# Helper function to apply replacements
def apply_replacements(text: str) -> str:
    if not text:
        return text
    for old, new in config['replacements'].items():
        # Use regex for links to ensure exact matches
        if old.startswith('http'):
            text = re.sub(re.escape(old), new, text)
        else:
            # For words/sentences, use word boundaries
            text = re.sub(r'\b' + re.escape(old) + r'\b', new, text)
    return text

# Message handler for forwarding
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config['forwarding_active'] or not config['target_channel']:
        return

    message = update.message
    if not message:
        return

    # Check if message is from a source channel
    if str(message.chat_id) not in [str(ch) for ch in config['source_channels']]:
        return

    try:
        # Handle different message types
        if message.text:
            text = apply_replacements(message.text)
            await bot.send_message(chat_id=config['target_channel'], text=text)
        elif message.photo:
            caption = apply_replacements(message.caption or '')
            await bot.send_photo(chat_id=config['target_channel'], photo=message.photo[-1].file_id, caption=caption)
        elif message.video:
            caption = apply_replacements(message.caption or '')
            await bot.send_video(chat_id=config['target_channel'], video=message.video.file_id, caption=caption)
        elif message.document:
            caption = apply_replacements(message.caption or '')
            await bot.send_document(chat_id=config['target_channel'], document=message.document.file_id, caption=caption)
        elif message.sticker:
            await bot.send_sticker(chat_id=config['target_channel'], sticker=message.sticker.file_id)
        elif message.audio:
            caption = apply_replacements(message.caption or '')
            await bot.send_audio(chat_id=config['target_channel'], audio=message.audio.file_id, caption=caption)
        elif message.voice:
            caption = apply_replacements(message.caption or '')
            await bot.send_voice(chat_id=config['target_channel'], voice=message.voice.file_id, caption=caption)
        elif message.video_note:
            await bot.send_video_note(chat_id=config['target_channel'], video_note=message.video_note.file_id)
        elif message.animation:
            caption = apply_replacements(message.caption or '')
            await bot.send_animation(chat_id=config['target_channel'], animation=message.animation.file_id, caption=caption)
        else:
            logger.warning(f"Unsupported message type from chat {message.chat_id}")
    except TelegramError as e:
        logger.error(f"Error forwarding message: {e}")

# Restrict commands to admin only
def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if config['admin_id'] is None:
            await update.message.reply_text("Admin ID not set. Please set it in the config.")
            return
        if update.message.from_user.id != config['admin_id']:
            await update.message.reply_text("You are not authorized to use this command.")
            return
        return await handler(update, context)
    return wrapper

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the Auto-Forwarder Bot!\n"
        "Available commands: \n"
        "/start or /help - Show this message\n"
        "/addreplace <channel> - Add a replacement rule\n"
        "/removereplace <old> - Remove a replacement rule\n"
        "/listreplace - List all replacement rules\n"
        "/addsource <channel> - Add a source channel\n"
        "/removesource <channel> - Remove a source channel\n"
        "/settarget <channel> - Set the target channel\n"
        "/startforward - Start forwarding\n"
        "/stopforward - Stop forwarding\n"
        "/status - Check bot status"
    )

@admin_only
async def add_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or '->' not in ' '.join(context.args):
        await update.message.reply_text("Usage: /addreplace <old> -> <new>")
        return
    text = ' '.join(context.args)
    old, new = text.split('->', 1)
    old, new = old.strip(), new.strip()
    config['replacements'][old] = new
    save_config(config)
    await update.message.reply_text(f"Added replacement: '{old}' -> '{new}'")

@admin_only
async def remove_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removereplace <old>")
        return
    old = ' '.join(context.args).strip()
    if old in config['replacements']:
        del config['replacements'][old]
        save_config(config)
        await update.message.reply_text(f"Removed replacement: '{old}'")
    else:
        await update.message.reply_text(f"No replacement found for: '{old}'")

@admin_only
async def list_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config['replacements']:
        await update.message.reply_text("No replacement rules set.")
        return
    replacements = '\n'.join(f"'{old}' -> '{new}'" for old, new in config['replacements'].items())
    await update.message.reply_text(f"Replacement rules:\n{replacements}")

@admin_only
async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addsource <channel>")
        return
    channel = context.args[0].strip()
    chat_id = await get_chat_id(channel)
    if not chat_id:
        await update.message.reply_text(f"Could not resolve channel: {channel}")
        return
    if str(chat_id) not in config['source_channels']:
        config['source_channels'].append(str(chat_id))
        save_config(config)
        await update.message.reply_text(f"Added source channel: {channel} (ID: {chat_id})")
    else:
        await update.message.reply_text(f"Channel {channel} is already a source.")

@admin_only
async def remove_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removesource <channel>")
        return
    channel = context.args[0].strip()
    chat_id = await get_chat_id(channel)
    if not chat_id:
        await update.message.reply_text(f"Could not resolve channel: {channel}")
        return
    if str(chat_id) in config['source_channels']:
        config['source_channels'].remove(str(chat_id))
        save_config(config)
        await update.message.reply_text(f"Removed source channel: {channel}")
    else:
        await update.message.reply_text(f"Channel {channel} is not a source.")

@admin_only
async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /settarget <channel>")
        return
    channel = context.args[0].strip()
    chat_id = await get_chat_id(channel)
    if not chat_id:
        await update.message.reply_text(f"Could not resolve channel: {channel}")
        return
    config['target_channel'] = str(chat_id)
    save_config(config)
    await update.message.reply_text(f"Set target channel: {channel} (ID: {chat_id})")

@admin_only
async def start_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config['source_channels'] or not config['target_channel']:
        await update.message.reply_text("Please set source and target channels first.")
        return
    config['forwarding_active'] = True
    save_config(config)
    await update.message.reply_text("Forwarding started.")

@admin_only
async def stop_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config['forwarding_active'] = False
    save_config(config)
    await update.message.reply_text("Forwarding stopped.")

@admin_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = (
        f"Bot Status:\n"
        f"Forwarding Active: {config['forwarding_active']}\n"
        f"Source Channels: {', '.join(config['source_channels']) or 'None'}\n"
        f"Target Channel: {config['target_channel'] or 'None'}\n"
        f"Replacement Rules: {len(config['replacements'])} set\n"
        f"Admin ID: {config['admin_id'] or 'Not set'}"
    )
    await update.message.reply_text(status)

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    # Replace with your Telegram user ID
    if config['admin_id'] is None:
        print("Please set your Telegram user ID in bot_config.json")
        return

    # Initialize application
    application = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', start))
    application.add_handler(CommandHandler('addreplace', add_replace))
    application.add_handler(CommandHandler('removereplace', remove_replace))
    application.add_handler(CommandHandler('listreplace', list_replace))
    application.add_handler(CommandHandler('addsource', add_source))
    application.add_handler(CommandHandler('removesource', remove_source))
    application.add_handler(CommandHandler('settarget', set_target))
    application.add_handler(CommandHandler('startforward', start_forward))
    application.add_handler(CommandHandler('stopforward', stop_forward))
    application.add_handler(CommandHandler('status', status))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, forward_message))
    application.add_error_handler(error_handler)

    # Start the bot
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
