import json
import logging
import os
from dotenv import load_dotenv
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

REPLACEMENTS_FILE = "replacements.json"
CONFIG_FILE = "config.json"


# Load config
def load_json(file, default={}):
    if not os.path.exists(file):
        with open(file, 'w') as f: json.dump(default, f, indent=2)
    with open(file, 'r') as f: return json.load(f)

def save_json(file, data):
    with open(file, 'w') as f: json.dump(data, f, indent=2)

replacements = load_json(REPLACEMENTS_FILE, {"links": {}, "words": {}, "sentences": {}})
config = load_json(CONFIG_FILE, {"source_channel": "", "target_channel": ""})


def apply_replacements(text):
    for old, new in replacements["links"].items():
        text = text.replace(old, new)
    for old, new in replacements["words"].items():
        text = text.replace(old, new)
    for old, new in replacements["sentences"].items():
        text = text.replace(old, new)
    return text


# Forward handler
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.channel_post: return
    msg = update.channel_post
    if msg.chat.username != config["source_channel"].lstrip("@"): return

    try:
        if msg.text or msg.caption:
            modified_text = apply_replacements(msg.text or msg.caption)
            await context.bot.send_message(chat_id=config["target_channel"], text=modified_text)
        elif msg.photo:
            await context.bot.send_photo(chat_id=config["target_channel"], photo=msg.photo[-1].file_id, caption=apply_replacements(msg.caption or ""))
        elif msg.video:
            await context.bot.send_video(chat_id=config["target_channel"], video=msg.video.file_id, caption=apply_replacements(msg.caption or ""))
        else:
            await msg.copy(chat_id=config["target_channel"])
    except Exception as e:
        print("Error forwarding message:", e)


# Admin commands
async def check_admin(update: Update):
    return update.effective_user.id == ADMIN_ID


async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        args = " ".join(context.args)
        old, new = args.split("=>")
        replacements["links"][old.strip()] = new.strip()
        save_json(REPLACEMENTS_FILE, replacements)
        await update.message.reply_text(f"✅ Link replaced:\n{old.strip()} => {new.strip()}")
    except:
        await update.message.reply_text("❌ Format: /addlink old => new")


async def remove_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        word = context.args[0]
        if word in replacements["words"]:
            del replacements["words"][word]
            save_json(REPLACEMENTS_FILE, replacements)
            await update.message.reply_text(f"✅ Removed word: {word}")
        else:
            await update.message.reply_text("❌ Word not found.")
    except:
        await update.message.reply_text("❌ Format: /removeword word")


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



# Main function
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, forward_message))
    app.add_handler(CommandHandler("addlink", add_link))
    app.add_handler(CommandHandler("removeword", remove_word))
    app.add_handler(CommandHandler("setsrc", set_source))
    app.add_handler(CommandHandler("setdst", set_target))
    app.add_handler(CommandHandler("showreplacements", show_replacements))

    app.run_polling()


if __name__ == "__main__":
    main()
