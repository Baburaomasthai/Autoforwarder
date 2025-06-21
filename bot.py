import os
import json
import re
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from flask import Flask
from threading import Thread
from filelock import FileLock
import asyncio

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
ADMIN_IDS = json.loads(os.getenv("ADMIN_IDS", "[]"))  # e.g., "[123456789, 987654321]"

# Configuration files
REPLACEMENTS_FILE = "replacements.json"
CHANNELS_FILE = "channels.json"
STATUS_FILE = "status.json"

# Initialize Pyrogram client
app = Client("autoforward_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Flask for keep-alive (e.g., Replit, Railway, Render)
flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

Thread(target=run_flask, daemon=True).start()

# Load or create JSON data files
def load_json(path, default):
    with FileLock(path + ".lock"):
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default, f, indent=2)
        with open(path, "r") as f:
            return json.load(f)

def save_json(path, data):
    with FileLock(path + ".lock"):
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

# Initialize configuration
replacements = load_json(REPLACEMENTS_FILE, {})
channels = load_json(CHANNELS_FILE, {"sources": [], "target": None})
status = load_json(STATUS_FILE, {"forwarding": False})

# Validate channel (username or chat ID)
async def validate_channel(channel):
    try:
        chat = await app.get_chat(channel)
        return str(chat.id)
    except Exception as e:
        logger.error(f"Failed to validate channel {channel}: {e}")
        return None

# Apply text replacements with regex
def replace_text(text):
    if not text:
        return text
    for key, value in replacements.items():
        if key.startswith(('http://', 'https://')):
            text = re.sub(re.escape(key), value, text)
        else:
            text = re.sub(r'\b' + re.escape(key) + r'\b', value, text)
    return text

# Forward messages with rate limit handling
async def forward_message_with_retry(message, target, max_retries=3):
    for attempt in range(max_retries):
        try:
            if message.text:
                new_text = replace_text(message.text)
                await app.send_message(target, new_text)
            elif message.photo:
                new_caption = replace_text(message.caption or "")
                await app.send_photo(target, message.photo.file_id, caption=new_caption)
            elif message.video:
                new_caption = replace_text(message.caption or "")
                await app.send_video(target, message.video.file_id, caption=new_caption)
            elif message.document:
                new_caption = replace_text(message.caption or "")
                await app.send_document(target, message.document.file_id, caption=new_caption)
            elif message.sticker:
                await app.send_sticker(target, message.sticker.file_id)
            elif message.audio:
                new_caption = replace_text(message.caption or "")
                await app.send_audio(target, message.audio.file_id, caption=new_caption)
            elif message.voice:
                new_caption = replace_text(message.caption or "")
                await app.send_voice(target, message.voice.file_id, caption=new_caption)
            elif message.video_note:
                await app.send_video_note(target, message.video_note.file_id)
            elif message.animation:
                new_caption = replace_text(message.caption or "")
                await app.send_animation(target, message.animation.file_id, caption=new_caption)
            else:
                await message.copy(target)  # Fallback for unsupported types
            return
        except FloodWait as e:
            logger.warning(f"FloodWait: Waiting {e.value} seconds")
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.error(f"Error forwarding message: {e}")
            for admin_id in ADMIN_IDS:
                await app.send_message(admin_id, f"⚠️ Error forwarding message: {e}")
            return
    logger.error("Max retries reached for forwarding message")
    for admin_id in ADMIN_IDS:
        await app.send_message(admin_id, "⚠️ Max retries reached for forwarding message")

# Forwarder handler
@app.on_message(filters.channel)
async def forward_all(client, message: Message):
    if not status["forwarding"] or not channels["target"]:
        return

    # Check if message is from a source channel
    if str(message.chat.id) not in channels["sources"]:
        return

    await forward_message_with_retry(message, channels["target"])

# Admin commands
@app.on_message(filters.private & filters.user(ADMIN_IDS))
async def admin_commands(client, message: Message):
    text = message.text.lower()
    args = message.text.split()

    if text.startswith(("/start", "/help")):
        await message.reply(
            "✅ Telegram Auto-Forwarder Bot\n\n"
            "Commands:\n"
            "/start, /help - Show this message\n"
            "/ping - Check bot responsiveness\n"
            "/addreplace old -> new - Add replacement rule\n"
            "/removereplace old - Remove replacement rule\n"
            "/clearreplacements - Clear all replacements\n"
            "/listreplace - List replacements\n"
            "/addsource channel - Add source channel\n"
            "/removesource channel - Remove source channel\n"
            "/listsource - List source channels\n"
            "/settarget channel - Set target channel\n"
            "/gettarget - Show target channel\n"
            "/startfwd - Start forwarding\n"
            "/stop - Stop forwarding\n"
            "/status - Check bot status"
        )

    elif text.startswith("/ping"):
        await message.reply("🏓 Pong! Bot is alive.")

    elif text.startswith("/addreplace"):
        try:
            parts = message.text.split(" -> ", 1)
            if len(parts) != 2:
                raise ValueError("Invalid format")
            old, new = parts[1].split(" ", 1)
            replacements[old] = new
            save_json(REPLACEMENTS_FILE, replacements)
            await message.reply(f"🔁 Added replacement: '{old}' -> '{new}'")
        except:
            await message.reply("❌ Usage: `/addreplace old -> new`")

    elif text.startswith("/removereplace"):
        try:
            key = message.text.split(" ", 1)[1]
            if key in replacements:
                del replacements[key]
                save_json(REPLACEMENTS_FILE, replacements)
                await message.reply(f"❌ Removed replacement: '{key}'")
            else:
                await message.reply(f"❌ No replacement found for: '{key}'")
        except:
            await message.reply("❌ Usage: `/removereplace old`")

    elif text.startswith("/clearreplacements"):
        replacements.clear()
        save_json(REPLACEMENTS_FILE, replacements)
        await message.reply("🧹 All replacements cleared.")

    elif text.startswith("/listreplace"):
        if replacements:
            reply = "🔁 Replacements:\n" + "\n".join([f"'{k}' ➤ '{v}'" for k, v in replacements.items()])
        else:
            reply = "No replacements set."
        await message.reply(reply)

    elif text.startswith("/addsource"):
        try:
            channel = args[1]
            chat_id = await validate_channel(channel)
            if not chat_id:
                await message.reply(f"❌ Invalid or inaccessible channel: {channel}")
                return
            if chat_id not in channels["sources"]:
                channels["sources"].append(chat_id)
                save_json(CHANNELS_FILE, channels)
                await message.reply(f"✅ Added source channel: {channel} (ID: {chat_id})")
            else:
                await message.reply(f"❌ Channel {channel} is already a source.")
        except:
            await message.reply("❌ Usage: `/addsource @username or chat_id`")

    elif text.startswith("/removesource"):
        try:
            channel = args[1]
            chat_id = await validate_channel(channel)
            if not chat_id:
                await message.reply(f"❌ Invalid or inaccessible channel: {channel}")
                return
            if chat_id in channels["sources"]:
                channels["sources"].remove(chat_id)
                save_json(CHANNELS_FILE, channels)
                await message.reply(f"❌ Removed source channel: {channel}")
            else:
                await message.reply(f"❌ Channel {channel} is not a source.")
        except:
            await message.reply("❌ Usage: `/removesource @username or chat_id`")

    elif text.startswith("/listsource"):
        reply = "📡 Source Channels:\n" + "\n".join(channels["sources"]) if channels["sources"] else "No sources set."
        await message.reply(reply)

    elif text.startswith("/settarget"):
        try:
            channel = args[1]
            chat_id = await validate_channel(channel)
            if not chat_id:
                await message.reply(f"❌ Invalid or inaccessible channel: {channel}")
                return
            channels["target"] = chat_id
            save_json(CHANNELS_FILE, channels)
            await message.reply(f"🎯 Set target channel: {channel} (ID: {chat_id})")
        except:
            await message.reply("❌ Usage: `/settarget @username or chat_id`")

    elif text.startswith("/gettarget"):
        target = channels["target"]
        await message.reply(f"🎯 Current target: {target}" if target else "No target set.")

    elif text.startswith("/startfwd"):
        if not channels["sources"] or not channels["target"]:
            await message.reply("❌ Set source and target channels first.")
            return
        status["forwarding"] = True
        save_json(STATUS_FILE, status)
        await message.reply("✅ Forwarding started.")

    elif text.startswith("/stop"):
        status["forwarding"] = False
        save_json(STATUS_FILE, status)
        await message.reply("⛔ Forwarding stopped.")

    elif text.startswith("/status"):
        status_text = (
            f"Bot Status:\n"
            f"Forwarding: {'ON' if status['forwarding'] else 'OFF'}\n"
            f"Source Channels: {', '.join(channels['sources']) or 'None'}\n"
            f"Target Channel: {channels['target'] or 'None'}\n"
            f"Replacements: {len(replacements)}"
        )
        await message.reply(status_text)

    else:
        await message.reply("❌ Unknown command. Use /help for commands.")

# Start bot
if __name__ == "__main__":
    if not all([BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS]):
        logger.error("Missing environment variables: BOT_TOKEN, API_ID, API_HASH, or ADMIN_IDS")
        exit(1)
    logger.info("Starting Telegram Auto-Forwarder Bot...")
    app.run()
