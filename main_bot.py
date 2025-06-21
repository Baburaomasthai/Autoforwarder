import json
import logging
import os
import time
import html
import feedparser
import threading
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler
)

# Load environment variables
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

REPLACEMENTS_FILE = "replacements.json"
CONFIG_FILE = "config.json"

# Load JSON helpers
def load_json(file, default={}):
    if not os.path.exists(file):
        with open(file, 'w') as f: json.dump(default, f, indent=2)
    with open(file, 'r') as f: return json.load(f)

def save_json(file, data):
    with open(file, 'w') as f: json.dump(data, f, indent=2)

# Load data
replacements = load_json(REPLACEMENTS_FILE, {"links": {}, "words": {}, "sentences": {}})
config = load_json(CONFIG_FILE, {"source_channel": "", "target_channel": ""})

# Text replacement logic
def apply_replacements(text):
    for old, new in replacements.get("links", {}).items():
        text = text.replace(old, new)
    for old, new in replacements.get("words", {}).items():
        text = text.replace(old, new)
    for old, new in replacements.get("sentences", {}).items():
        text = text.replace(old, new)
    return text

# 🔁 RSS Forwarding Function
def rss_forwarder():
    print("📡 RSS Forwarder started...")
    bot = Bot(token=BOT_TOKEN)
    sent_links = []
    while True:
        if not config["source_channel"] or not config["target_channel"]:
            time.sleep(10)
            continue

        rss_url = f"https://rsshub.app/telegram/channel/{config['source_channel'].lstrip('@')}"
        try:
            feed = feedparser.parse(rss_url)
            for entry in reversed(feed.entries):
                link = entry.link
                if link in sent_links:
                    continue

                text = html.unescape(entry.title)
                text = apply_replacements(text)

                try:
                    bot.send_message(chat_id=config["target_channel"], text=text)
                    sent_links.append(link)
                    print("✅ Forwarded:", text)
                except Exception as e:
                    print("❌ Error sending message:", e)

            time.sleep(15)
        except Exception as e:
            print("❌ Error fetching RSS:", e)
            time.sleep(20)

# 🧠 Admin Check
async def check_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

# ✅ Admin Commands
async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        old, new = " ".join(context.args).split("=>")
        replacements["links"][old.strip()] = new.strip()
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Link replaced:\n{old.strip()} => {new.strip()}")
    except:
        await update.message.reply_text("❌ Format: /addlink old => new")

async def add_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        old, new = " ".join(context.args).split("=>")
        replacements["words"][old.strip()] = new.strip()
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Word replaced:\n{old.strip()} => {new.strip()}")
    except:
        await update.message.reply_text("❌ Format: /addword old => new")

async def add_sentence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        old, new = " ".join(context.args).split("=>")
        replacements["sentences"][old.strip()] = new.strip()
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Sentence replaced:\n{old.strip()} => {new.strip()}")
    except:
        await update.message.reply_text("❌ Format: /addsentence old => new")

async def removeword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    word = context.args[0]
    if word in replacements["words"]:
        del replacements["words"][word]
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Removed word: {word}")
    else:
        await update.message.reply_text("❌ Word not found.")

async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    config["source_channel"] = context.args[0]
    save_json(CONFIG_FILE, config)
    await update.message.reply_text(f"✅ Source channel set: {config['source_channel']}")

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    config["target_channel"] = context.args[0]
    save_json(CONFIG_FILE, config)
    await update.message.reply_text(f"✅ Target channel set: {config['target_channel']}")

async def show_replacements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    await update.message.reply_text(f"📌 Replacements:\n```json\n{json.dumps(replacements, indent=2)}```", parse_mode="Markdown")

# 🏁 Main Function
def main():
    # Start RSS Thread
    threading.Thread(target=rss_forwarder, daemon=True).start()

    # Start Telegram Command Bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("addlink", add_link))
    app.add_handler(CommandHandler("addword", add_word))
    app.add_handler(CommandHandler("addsentence", add_sentence))
    app.add_handler(CommandHandler("removeword", removeword))
    app.add_handler(CommandHandler("setsrc", set_source))
    app.add_handler(CommandHandler("setdst", set_target))
    app.add_handler(CommandHandler("showreplacements", show_replacements))

    print("✅ Bot Started. Waiting for admin commands...")
    app.run_polling()

if __name__ == "__main__":
    main()
