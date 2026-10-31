import asyncio
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime, timezone
from functools import wraps

from bson import ObjectId
from flask import Flask
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage

# ----------------------------------------------------------------------------
# Keep-alive web server (Render etc.)
# ----------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "✅ 𝑇𝑟𝑦𝑖𝑛𝑔 𝑇𝑜 𝑇𝑎𝑐𝑘𝑙𝑒 𝑆𝑒𝑡𝑏𝑎𝑐𝑘 𝑇𝐺 - @MrJaggiX!"


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


threading.Thread(target=run_flask, daemon=True).start()

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def require_env(name):
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing environment variable: {name}")
    return value


API_ID = int(require_env("API_ID"))
API_HASH = require_env("API_HASH")
SESSION_STRING = require_env("SESSION_STRING")
TOKEN = require_env("BOT_TOKEN")
OWNER_USER_ID = int(require_env("ADMIN_USER_ID"))
MONGODB_URL = require_env("MONGODB_URL")  # never hardcode credentials
MAPPING_TTL_DAYS = int(os.getenv("MAPPING_TTL_DAYS", "30"))

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
mongo_client = AsyncIOMotorClient(MONGODB_URL)
db = mongo_client.auto_forward_bot

DEFAULT_SETTINGS = [
    {"name": "forwarding_enabled", "value": "true"},
    {"name": "edit_sync", "value": "false"},
    {"name": "delete_sync", "value": "false"},
    {"name": "text_only", "value": "false"},
    {"name": "max_concurrent_tasks", "value": "10"},
]
TOGGLES = ("forwarding_enabled", "edit_sync", "delete_sync", "text_only")


async def ensure_default_settings():
    for setting in DEFAULT_SETTINGS:
        await db.settings.update_one({"name": setting["name"]}, {"$setOnInsert": setting}, upsert=True)


async def init_db():
    try:
        existing = await db.list_collection_names()
        for name in ["channel_pairs", "edits", "links", "settings", "admins", "message_mapping"]:
            if name not in existing:
                await db.create_collection(name)

        await db.channel_pairs.create_index([("source_id", ASCENDING), ("target_id", ASCENDING)], unique=True)
        await db.message_mapping.create_index([("source_channel_id", ASCENDING), ("source_message_id", ASCENDING)])
        await db.settings.create_index("name", unique=True)
        await db.admins.create_index("user_id", unique=True)
        try:
            # Old mappings expire automatically so the collection never grows forever.
            await db.message_mapping.create_index("created_at", expireAfterSeconds=MAPPING_TTL_DAYS * 86400)
        except Exception as e:
            logger.warning(f"TTL index not created: {e}")

        await ensure_default_settings()
        await db.admins.update_one(
            {"user_id": OWNER_USER_ID},
            {"$setOnInsert": {"user_id": OWNER_USER_ID, "username": "Owner", "is_owner": True}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f"Error initializing database: {e}")


# ----------------------------------------------------------------------------
# Core state / database layer
# ----------------------------------------------------------------------------
class ForwardBot:
    def __init__(self):
        self.channel_pairs = {}    # source_id -> [target_id, ...]
        self.channel_filters = {}  # source_id -> {target_id: keywords}
        # settings (safe defaults, even if loading from DB fails)
        self.forwarding_enabled = True
        self.edit_sync = False
        self.delete_sync = False
        self.text_only = False
        self.max_concurrent_tasks = 10
        self.semaphore = None
        # cached rules (no DB hit per message)
        self.edits = []   # [(old_text, new_text)]
        self.links = []   # [(old_link, new_link)]
        self.me_id = None
        self._initialized = False

    def get_semaphore(self):
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
        return self.semaphore

    async def initialize(self):
        if self._initialized:
            return
        await self.load_settings()
        await self.load_channel_pairs()
        await self.refresh_rules()
        self._initialized = True

    # ---- settings ----
    def _apply_setting(self, name, value):
        if name == "forwarding_enabled":
            self.forwarding_enabled = value == "true"
        elif name == "edit_sync":
            self.edit_sync = value == "true"
        elif name == "delete_sync":
            self.delete_sync = value == "true"
        elif name == "text_only":
            self.text_only = value == "true"
        elif name == "max_concurrent_tasks":
            self.max_concurrent_tasks = int(value)
            self.semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

    async def load_settings(self):
        try:
            async for doc in db.settings.find({}):
                self._apply_setting(doc["name"], doc["value"])
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
        self.get_semaphore()

    async def set_setting(self, name, value):
        try:
            await db.settings.update_one({"name": name}, {"$set": {"value": value}}, upsert=True)
            self._apply_setting(name, value)
        except Exception as e:
            logger.error(f"Error setting setting: {e}")

    # ---- channel pairs ----
    async def load_channel_pairs(self):
        self.channel_pairs = {}
        self.channel_filters = {}
        try:
            async for doc in db.channel_pairs.find({}):
                src, tgt = doc["source_id"], doc["target_id"]
                self.channel_pairs.setdefault(src, []).append(tgt)
                self.channel_filters.setdefault(src, {})[tgt] = doc.get("filter_keywords")
        except Exception as e:
            logger.error(f"Error loading channel pairs: {e}")

    async def add_channel_pair(self, source_id, target_id, filter_keywords=None):
        try:
            await db.channel_pairs.insert_one(
                {"source_id": source_id, "target_id": target_id, "filter_keywords": filter_keywords}
            )
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error adding channel pair: {e}")
            return False
        self.channel_pairs.setdefault(source_id, []).append(target_id)
        self.channel_filters.setdefault(source_id, {})[target_id] = filter_keywords
        return True

    async def update_channel_filter(self, source_id, target_id, filter_keywords):
        try:
            await db.channel_pairs.update_one(
                {"source_id": source_id, "target_id": target_id},
                {"$set": {"filter_keywords": filter_keywords}},
            )
            if target_id in self.channel_filters.get(source_id, {}):
                self.channel_filters[source_id][target_id] = filter_keywords
            return True
        except Exception as e:
            logger.error(f"Error updating filter: {e}")
            return False

    async def remove_channel_pair(self, source_id, target_id=None):
        """Returns number of removed pairs."""
        try:
            query = {"source_id": source_id}
            if target_id is not None:
                query["target_id"] = target_id
            result = await db.channel_pairs.delete_many(query)

            if source_id in self.channel_pairs:
                if target_id is not None:
                    if target_id in self.channel_pairs[source_id]:
                        self.channel_pairs[source_id].remove(target_id)
                    self.channel_filters.get(source_id, {}).pop(target_id, None)
                    if not self.channel_pairs[source_id]:
                        del self.channel_pairs[source_id]
                        self.channel_filters.pop(source_id, None)
                else:
                    del self.channel_pairs[source_id]
                    self.channel_filters.pop(source_id, None)
            return result.deleted_count
        except Exception as e:
            logger.error(f"Error removing channel pair: {e}")
            return 0

    def get_all_channel_pairs(self):
        pairs = []
        for source_id, target_ids in self.channel_pairs.items():
            for target_id in target_ids:
                pairs.append((source_id, target_id, self.channel_filters.get(source_id, {}).get(target_id)))
        return pairs

    def should_forward_message(self, message_text, source_id, target_id):
        filter_keywords = self.channel_filters.get(source_id, {}).get(target_id)
        if not filter_keywords or not filter_keywords.strip():
            return True
        keywords = [kw.strip().lower() for kw in filter_keywords.split(",") if kw.strip()]
        text = (message_text or "").lower()
        return any(kw in text for kw in keywords)

    # ---- replacement rules (cached) ----
    async def refresh_rules(self):
        try:
            self.edits = [(d["old_text"], d["new_text"]) async for d in db.edits.find({})]
            self.links = [(d["old_link"], d["new_link"]) async for d in db.links.find({})]
        except Exception as e:
            logger.error(f"Error refreshing rules: {e}")

    # ---- message mapping (DB only, TTL-expired) ----
    async def save_message_mapping(self, source_message_id, source_channel_id, target_message_id, target_channel_id):
        try:
            await db.message_mapping.update_one(
                {
                    "source_message_id": source_message_id,
                    "source_channel_id": source_channel_id,
                    "target_channel_id": target_channel_id,
                },
                {"$set": {"target_message_id": target_message_id, "created_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error saving message mapping: {e}")
            return False

    async def get_message_mapping(self, source_channel_id, source_message_id):
        result = {}
        try:
            async for doc in db.message_mapping.find(
                {"source_channel_id": source_channel_id, "source_message_id": source_message_id}
            ):
                result[doc["target_channel_id"]] = doc["target_message_id"]
        except Exception as e:
            logger.error(f"Error reading message mapping: {e}")
        return result

    async def delete_message_mapping(self, source_message_id, source_channel_id):
        try:
            await db.message_mapping.delete_many(
                {"source_message_id": source_message_id, "source_channel_id": source_channel_id}
            )
        except Exception as e:
            logger.error(f"Error deleting message mapping: {e}")

    # ---- admins ----
    async def is_admin(self, user_id):
        try:
            return await db.admins.find_one({"user_id": user_id}) is not None
        except Exception as e:
            logger.error(f"Error checking admin: {e}")
            return False

    async def is_owner(self, user_id):
        try:
            return await db.admins.find_one({"user_id": user_id, "is_owner": True}) is not None
        except Exception as e:
            logger.error(f"Error checking owner: {e}")
            return False

    async def add_admin(self, user_id, username):
        try:
            await db.admins.insert_one({"user_id": user_id, "username": username, "is_owner": False})
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
            return False

    async def remove_admin(self, user_id):
        if await self.is_owner(user_id):
            return False
        try:
            result = await db.admins.delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing admin: {e}")
            return False

    async def get_all_admins(self):
        try:
            return [(d["user_id"], d.get("username", "Unknown"), d.get("is_owner", False)) async for d in db.admins.find({})]
        except Exception as e:
            logger.error(f"Error getting admins: {e}")
            return []


bot = ForwardBot()

# ----------------------------------------------------------------------------
# Content rules: word replacement + link allow-list
# ----------------------------------------------------------------------------
TLDS = (
    "com|net|org|io|co|me|info|biz|xyz|app|dev|online|site|store|shop|link|live|top|club|"
    "ly|gl|gg|tv|ai|in|to|cc|ws|page|tech|cloud|click|fun|one|pro|vip|win|cam|bet"
)

LINK_PATTERN = re.compile(
    r"(?P<md>\[(?P<label>[^\]]*)\]\((?P<href>[^)\s]*)\))"          # [text](url)  (hidden hyperlinks)
    r"|(?P<url>"
    r"(?:https?://|www\.)[^\s<>\"']+"                                # http(s):// and www.
    r"|\b(?:t\.me|telegram\.me)/[^\s<>\"']+"                         # t.me/...
    rf"|(?<![\w@.])(?:[a-z0-9-]+\.)+(?:{TLDS})\b(?:/[^\s<>\"']*)?"   # bare domains with a real TLD
    r")",
    re.IGNORECASE,
)
TRAILING_PUNCT = ".,!?;:)]}"


def normalize_link(link):
    link = (link or "").strip().rstrip(TRAILING_PUNCT).lower().rstrip("/")
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", link)


def strip_links(text, approved):
    """Keep only approved links (swapped for their replacement); drop everything else."""

    def repl(match):
        if match.group("md") is not None:
            label, href = match.group("label"), match.group("href")
            if href.lower().startswith("tg://"):      # mentions - keep as is
                return match.group(0)
            new_link = approved.get(normalize_link(href))
            clean_label = strip_links(label, approved)
            if new_link is None:
                return clean_label
            return f"[{clean_label}]({new_link})"

        raw = match.group("url")
        core = raw.rstrip(TRAILING_PUNCT)
        trailing = raw[len(core):]
        new_link = approved.get(normalize_link(core))
        if new_link is None:
            return ""
        return new_link + trailing

    return LINK_PATTERN.sub(repl, text)


def apply_content_rules(message_text, edits, links):
    processed = message_text or ""
    for old_text, new_text in edits:
        if old_text:
            processed = processed.replace(old_text, new_text)
    approved = {normalize_link(old): new for old, new in links if old}
    return strip_links(processed, approved)


# ----------------------------------------------------------------------------
# Forwarding engine (Telethon)
# ----------------------------------------------------------------------------
async def with_flood_retry(func, *args, retries=3, **kwargs):
    for attempt in range(retries + 1):
        try:
            return await func(*args, **kwargs)
        except FloodWaitError as e:
            if attempt == retries:
                raise
            logger.warning(f"FloodWait: sleeping {e.seconds + 1}s")
            await asyncio.sleep(e.seconds + 1)


async def process_message(source_id, target_id, message_text, media=None):
    try:
        async with bot.get_semaphore():
            if not bot.should_forward_message(message_text, source_id, target_id):
                logger.info(f"Skipping message due to filter: {source_id} -> {target_id}")
                return None

            text = apply_content_rules(message_text, bot.edits, bot.links)

            if bot.text_only:
                media = None
            if media is not None and isinstance(media, MessageMediaWebPage):
                media = None  # link preview, not a real file

            if media is not None:
                return await with_flood_retry(client.send_file, target_id, media, caption=text)
            if not text.strip():
                return None
            return await with_flood_retry(client.send_message, target_id, text)
    except Exception as e:
        logger.error(f"Error forwarding message: {e}")
        return None


async def forward_single(source_message_id, source_id, target_id, text, media):
    sent = await process_message(source_id, target_id, text, media)
    if sent:
        await bot.save_message_mapping(source_message_id, source_id, sent.id, target_id)


async def forward_album(messages, source_id, target_id):
    try:
        caption = next((m.text for m in messages if m.text), "")
        async with bot.get_semaphore():
            if not bot.should_forward_message(caption, source_id, target_id):
                return
            text = apply_content_rules(caption, bot.edits, bot.links)

            files = [] if bot.text_only else [
                m.media for m in messages if m.media and not isinstance(m.media, MessageMediaWebPage)
            ]
            if files:
                sent = await with_flood_retry(client.send_file, target_id, files, caption=text)
                sent = sent if isinstance(sent, list) else [sent]
                for src_msg, sent_msg in zip(messages, sent):
                    await bot.save_message_mapping(src_msg.id, source_id, sent_msg.id, target_id)
            elif text.strip():
                sent = await with_flood_retry(client.send_message, target_id, text)
                await bot.save_message_mapping(messages[0].id, source_id, sent.id, target_id)
    except Exception as e:
        logger.error(f"Error forwarding album: {e}")


@client.on(events.NewMessage())
async def handle_new_message(event):
    if not bot.forwarding_enabled:
        return
    try:
        source_id = event.chat_id
        if source_id not in bot.channel_pairs:
            return
        msg = event.message
        if msg.action or msg.grouped_id:   # service messages / albums are handled elsewhere
            return
        if bot.me_id is not None and msg.sender_id == bot.me_id:  # avoid loops
            return

        text = msg.text or ""
        media = None if bot.text_only else msg.media
        await asyncio.gather(
            *(forward_single(msg.id, source_id, t, text, media) for t in list(bot.channel_pairs[source_id])),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Error in handle_new_message: {e}")


@client.on(events.Album())
async def handle_album(event):
    if not bot.forwarding_enabled:
        return
    try:
        source_id = event.chat_id
        if source_id not in bot.channel_pairs:
            return
        if bot.me_id is not None and event.messages[0].sender_id == bot.me_id:
            return
        await asyncio.gather(
            *(forward_album(event.messages, source_id, t) for t in list(bot.channel_pairs[source_id])),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Error in handle_album: {e}")


@client.on(events.MessageEdited())
async def handle_edit(event):
    if not bot.forwarding_enabled or not bot.edit_sync:
        return
    try:
        source_id = event.chat_id
        mapping = await bot.get_message_mapping(source_id, event.message.id)
        if not mapping:
            return
        text = apply_content_rules(event.message.text or "", bot.edits, bot.links)
        if not text.strip():
            return
        await asyncio.gather(
            *(with_flood_retry(client.edit_message, t, mid, text) for t, mid in mapping.items()),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Error in handle_edit: {e}")


@client.on(events.MessageDeleted())
async def handle_delete(event):
    if not bot.forwarding_enabled or not bot.delete_sync:
        return
    try:
        source_id = event.chat_id
        if source_id is None:
            return
        for deleted_id in event.deleted_ids:
            mapping = await bot.get_message_mapping(source_id, deleted_id)
            if not mapping:
                continue
            await asyncio.gather(
                *(with_flood_retry(client.delete_messages, t, mid) for t, mid in mapping.items()),
                return_exceptions=True,
            )
            await bot.delete_message_mapping(deleted_id, source_id)
    except Exception as e:
        logger.error(f"Error in handle_delete: {e}")


# ----------------------------------------------------------------------------
# Actions (shared by slash commands and inline buttons)
# ----------------------------------------------------------------------------
def on_off(value):
    return "ON ✅" if value else "OFF ❌"


def clip(text, limit=3900):
    return text if len(text) <= limit else text[: limit - 20] + "\n… (truncated)"


async def act_addpair(source_id, target_id, filter_keywords=None):
    if await bot.add_channel_pair(source_id, target_id, filter_keywords):
        msg = f"✅ Channel pair added:\nSource: {source_id} → Target: {target_id}"
        if filter_keywords:
            msg += f"\nFilter: {filter_keywords}"
        return msg
    return "😃 This channel pair already exists"


async def act_setfilter(source_id, target_id, filter_keywords):
    if target_id not in bot.channel_pairs.get(source_id, []):
        return "🥱 Channel pair does not exist"
    if filter_keywords and filter_keywords.strip().lower() == "none":
        filter_keywords = None
    if await bot.update_channel_filter(source_id, target_id, filter_keywords):
        if filter_keywords:
            return f"✅ Filter updated:\nSource: {source_id} → Target: {target_id}\nFilter: {filter_keywords}"
        return f"✅ Filter removed:\nSource: {source_id} → Target: {target_id}"
    return "🥲 Error updating filter"


async def act_removepair(source_id, target_id=None):
    count = await bot.remove_channel_pair(source_id, target_id)
    if count == 0:
        return "🥱 Channel pair not found"
    if target_id is not None:
        return f"✅ Channel pair removed:\nSource: {source_id} → Target: {target_id}"
    return f"✅ All channel pairs removed for source: {source_id}"


async def act_addword(old_text, new_text):
    old_text, new_text = (old_text or "").strip(), (new_text or "").strip()
    if not old_text or not new_text:
        return "↳ Both old_text and new_text required"
    await db.edits.insert_one({"old_text": old_text, "new_text": new_text})
    await bot.refresh_rules()
    return f"✅ Replacement added:\n{old_text} → {new_text}"


async def act_addlink(old_link, new_link):
    old_link, new_link = (old_link or "").strip(), (new_link or "").strip()
    if not old_link or not new_link:
        return "❌ Both links required"
    await db.links.insert_one({"old_link": old_link, "new_link": new_link})
    await bot.refresh_rules()
    return f"✅ Link replacement added:\n{old_link} → {new_link}"


async def act_removeword(old_text):
    result = await db.edits.delete_one({"old_text": old_text})
    await bot.refresh_rules()
    return f"✅ Replacement removed: {old_text}" if result.deleted_count else f"❌ Not found: {old_text}"


async def act_removelink(old_link):
    result = await db.links.delete_one({"old_link": old_link})
    await bot.refresh_rules()
    return f"✅ Link replacement removed: {old_link}" if result.deleted_count else f"❌ Not found: {old_link}"


async def act_block(text_to_block):
    text_to_block = (text_to_block or "").strip()
    if not text_to_block:
        return "➢ Format: /block text_to_block"
    await db.edits.insert_one({"old_text": text_to_block, "new_text": ""})
    await bot.refresh_rules()
    return f"✅ Content blocked: {text_to_block}"


TOGGLE_LABELS = {
    "forwarding_enabled": "Forwarding",
    "edit_sync": "Edit Sync",
    "delete_sync": "Delete Sync",
    "text_only": "Text Only Mode",
}


async def act_toggle(name):
    new_status = not getattr(bot, name)
    await bot.set_setting(name, "true" if new_status else "false")
    msg = f"✅ {TOGGLE_LABELS[name]} turned {on_off(new_status)}"
    if name == "text_only" and new_status:
        msg += "\nअब केवल text messages ही forward होंगे, media files नहीं।"
    return msg


async def act_set_forwarding(enabled):
    await bot.set_setting("forwarding_enabled", "true" if enabled else "false")
    return f"✅ Forwarding turned {on_off(enabled)}"


async def act_maxtasks(value):
    if value < 1 or value > 50:
        return "Please enter a value between 1 and 50"
    await bot.set_setting("max_concurrent_tasks", str(value))
    return f"✅ Max concurrent tasks set to {value}"


async def act_addadmin(requester_id, user_id, username):
    if not await bot.is_owner(requester_id):
        return "➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗"
    if await bot.add_admin(user_id, username or "Unknown"):
        return f"✅ Admin added: {user_id} ({username or 'Unknown'})"
    return "🤔 This user is already an admin"


async def act_removeadmin(requester_id, user_id):
    if not await bot.is_owner(requester_id):
        return "➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗"
    if await bot.remove_admin(user_id):
        return f"✅ Admin removed: {user_id}"
    return "😎 Cannot remove the owner or user not found"


async def act_reset(requester_id):
    if not await bot.is_owner(requester_id):
        return "➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗"
    for coll in (db.channel_pairs, db.edits, db.links, db.settings, db.message_mapping):
        await coll.delete_many({})
    await ensure_default_settings()
    bot.forwarding_enabled, bot.edit_sync, bot.delete_sync, bot.text_only = True, False, False, False
    await bot.load_settings()
    await bot.load_channel_pairs()
    await bot.refresh_rules()
    return "✅ All settings reset successfully"


# ----------------------------------------------------------------------------
# Text builders
# ----------------------------------------------------------------------------
def format_pair_line(src, tgt, filt):
    return f"Source: {src} → Target: {tgt} | Filter: {filt if filt else 'None'}"


def settings_text():
    lines = [
        "⚙️ Settings",
        "",
        f"Forwarding: {on_off(bot.forwarding_enabled)}",
        f"Edit Sync: {on_off(bot.edit_sync)}",
        f"Delete Sync: {on_off(bot.delete_sync)}",
        f"Text Only Mode: {on_off(bot.text_only)}",
        f"Max Concurrent Tasks: {bot.max_concurrent_tasks}",
        "",
        "📡 Channel Pairs:",
    ]
    pairs = bot.get_all_channel_pairs()
    lines += [format_pair_line(*p) for p in pairs] or ["No channel pairs set up yet"]
    return "\n".join(lines)


def replacements_text():
    if not bot.edits and not bot.links:
        return "❌ No active replacements found"
    out = "📜 Active Replacements:\n\n"
    if bot.edits:
        out += "📝 Text Replacements:\n" + "\n".join(f"{o} → {n}" if n else f"{o} → (blocked)" for o, n in bot.edits) + "\n\n"
    if bot.links:
        out += "🔗 Link Replacements:\n" + "\n".join(f"{o} → {n}" for o, n in bot.links)
    return out


async def admins_text():
    admins = await bot.get_all_admins()
    if not admins:
        return "🤔 No admins found"
    return "👑 Admins:\n\n" + "\n".join(
        f"ID: {uid}, Name: {name}{' (Owner)' if owner else ''}" for uid, name, owner in admins
    )


HELP_TEXT = (
    "🤖 Auto Forward Bot Help\n\n"
    "📚 Setup:\n"
    "1. User account ko target channels me admin banao (post permission ke saath)\n"
    "2. 📡 Channel Pairs → ➕ Add Pair se source/target jodo\n"
    "3. Pair kholke 🎯 filter keywords lagao (optional)\n"
    "4. 🔁 Replacements se text/link replace ya block karo (optional)\n\n"
    "🔗 Link rule: sirf /addlink wale links forward hote hain, baaki sab links hata diye jaate hain.\n\n"
    "📝 Channel IDs negative hote hain (e.g. -100123456789). ID ke liye channel ka message @userinfobot ko forward karo.\n\n"
    "⌨️ Slash commands (buttons ke alawa):\n"
    "/menu /addpair /removepair /setfilter /listpairs\n"
    "/addword old=>new  /addlink old new  /removeword /removelink /showreps /block\n"
    "/forward on|off  /editsync  /deletesync  /textonly  /settings  /maxtasks  /stop\n"
    "/addadmin /removeadmin /listadmins (owner)  /reset (owner)  /cancel"
)

# ----------------------------------------------------------------------------
# Inline keyboard helpers & menu renderers
# ----------------------------------------------------------------------------


def kb(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows])


MENU_BTN = kb([("🏠 Main Menu", "m:main")])
CANCEL_KB = kb([("❌ Cancel", "c")])


async def render_main(uid):
    toggle = ("⏹ Stop Forwarding", "s:q") if bot.forwarding_enabled else ("▶️ Start Forwarding", "s:q")
    text = (
        "🤖 Auto Forward Bot — Control Panel\n\n"
        f"Forwarding: {on_off(bot.forwarding_enabled)}\n"
        f"Channel pairs: {len(bot.get_all_channel_pairs())}\n"
        f"Replacements: {len(bot.edits)} text · {len(bot.links)} links\n\n"
        "Neeche se option choose karo 👇"
    )
    rows = [
        [("📡 Channel Pairs", "m:pairs"), ("🔁 Replacements", "m:reps")],
        [("⚙️ Settings", "m:set"), ("👑 Admins", "m:adm")],
        [toggle],
        [("❓ Help", "m:help")],
    ]
    if await bot.is_owner(uid):
        rows[-1].append(("🗑 Reset All", "x:ask"))
    return text, kb(*rows)


async def render_pairs_menu(uid):
    return (
        f"📡 Channel Pairs ({len(bot.get_all_channel_pairs())})\n\nSource se target tak forwarding yahan manage karo.",
        kb(
            [("➕ Add Pair", "f:addpair"), ("📋 View / Manage", "p:list")],
            [("🗑 Remove All of a Source", "f:removesource")],
            [("🔙 Back", "m:main")],
        ),
    )


async def render_reps(uid):
    return (
        f"🔁 Replacements\n\nText: {len(bot.edits)} · Links: {len(bot.links)}",
        kb(
            [("➕ Add Word", "f:addword"), ("➕ Add Link", "f:addlink")],
            [("🚫 Block Content", "f:block"), ("📜 View All", "r:view")],
            [("➖ Remove Word", "r:rmw"), ("➖ Remove Link", "r:rml")],
            [("🔙 Back", "m:main")],
        ),
    )


async def render_settings(uid):
    rows = [
        [(f"Forwarding: {on_off(bot.forwarding_enabled)}", "s:t:forwarding_enabled")],
        [(f"Edit Sync: {on_off(bot.edit_sync)}", "s:t:edit_sync")],
        [(f"Delete Sync: {on_off(bot.delete_sync)}", "s:t:delete_sync")],
        [(f"Text Only: {on_off(bot.text_only)}", "s:t:text_only")],
        [(f"⚡ Max Tasks: {bot.max_concurrent_tasks}", "s:mt")],
        [("🔙 Back", "m:main")],
    ]
    return settings_text(), kb(*rows)


async def render_admins(uid):
    text = await admins_text()
    return text, kb(
        [("➕ Add Admin", "f:addadmin"), ("➖ Remove Admin", "a:rm")],
        [("🔙 Back", "m:main")],
    )


async def render_help(uid):
    return HELP_TEXT, kb([("🔙 Back", "m:main")])


MENUS = {
    "main": render_main,
    "pairs": render_pairs_menu,
    "reps": render_reps,
    "set": render_settings,
    "adm": render_admins,
    "help": render_help,
}


def pairs_list_view():
    pairs = bot.get_all_channel_pairs()
    if not pairs:
        return "🥱 No channel pairs set up yet", kb([("➕ Add Pair", "f:addpair")], [("🔙 Back", "m:pairs")])
    text = "📡 Channel Pairs:\n\n" + "\n".join(format_pair_line(*p) for p in pairs) + "\n\nKisi pair par tap karke manage karo 👇"
    rows = [
        [(f"{src} → {tgt}{' 🎯' if filt else ''}", f"p:v:{src}:{tgt}")]
        for src, tgt, filt in pairs[:40]
    ]
    rows.append([("➕ Add Pair", "f:addpair"), ("🔙 Back", "m:pairs")])
    return text, kb(*rows)


def pair_detail_view(src, tgt):
    if tgt not in bot.channel_pairs.get(src, []):
        return "🥱 Channel pair does not exist", kb([("🔙 Back", "p:list")])
    filt = bot.channel_filters.get(src, {}).get(tgt)
    text = f"📡 Channel Pair\n\nSource: {src}\nTarget: {tgt}\nFilter: {filt if filt else 'None'}"
    return text, kb(
        [("✏️ Set Filter", f"f:setfilter:{src}:{tgt}"), ("🧹 Clear Filter", f"p:c:{src}:{tgt}")],
        [("🗑 Remove Pair", f"p:r:{src}:{tgt}")],
        [("🔙 Back", "p:list")],
    )


async def removal_list_view(kind):
    """kind: 'w' (text replacements / blocks) or 'l' (link replacements)."""
    if kind == "w":
        docs = [d async for d in db.edits.find({})]
        title = "➖ Remove Word — jise hatana hai us par tap karo"
        rows = [
            [((f"🚫 {d['old_text']}" if not d["new_text"] else f"{d['old_text']} → {d['new_text']}")[:55], f"r:dw:{d['_id']}")]
            for d in docs[:40]
        ]
    else:
        docs = [d async for d in db.links.find({})]
        title = "➖ Remove Link — jise hatana hai us par tap karo"
        rows = [[(f"{d['old_link']} → {d['new_link']}"[:55], f"r:dl:{d['_id']}")] for d in docs[:40]]
    if not docs:
        return "❌ Kuch bhi remove karne ke liye nahi hai", kb([("🔙 Back", "m:reps")])
    rows.append([("🔙 Back", "m:reps")])
    return title, kb(*rows)


async def admin_removal_view():
    admins = [a for a in await bot.get_all_admins() if not a[2]]
    if not admins:
        return "🤔 Remove karne layak koi admin nahi hai", kb([("🔙 Back", "m:adm")])
    rows = [[(f"{name} ({uid})"[:55], f"a:d:{uid}")] for uid, name, _ in admins[:40]]
    rows.append([("🔙 Back", "m:adm")])
    return "➖ Kis admin ko remove karna hai?", kb(*rows)


async def show(update, text, markup=None):
    text = clip(text)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def with_notice(notice, text):
    return f"{notice}\n\n{text}" if notice else text


# ----------------------------------------------------------------------------
# Guided input flows (button → bot asks → user replies)
# ----------------------------------------------------------------------------
FLOWS = {
    "addpair": [
        ("➕ Add Pair — Step 1/3\n\nSource channel ID bhejo\nExample: -100123456789", "source"),
        ("Step 2/3\n\nTarget channel ID bhejo\nExample: -100987654321", "target"),
        ("Step 3/3\n\nFilter keywords bhejo (comma se alag: keyword1,keyword2)\nFilter nahi chahiye to 'skip' likho.", "filter"),
    ],
    "setfilter": [("✏️ Naye filter keywords bhejo (comma separated).\nFilter hatane ke liye 'none' likho.", "filter")],
    "removesource": [("🗑 Us source channel ka ID bhejo jiske saare pairs remove karne hain:", "source")],
    "addword": [("📝 Purana text bhejo (jo replace karna hai):", "old"), ("Ab naya text bhejo (jisse replace karna hai):", "new")],
    "addlink": [("🔗 Purana link bhejo:", "old"), ("Ab naya link bhejo:", "new")],
    "block": [("🚫 Jo text/content block karna hai wo bhejo:", "text")],
    "addadmin": [("👑 Naye admin ka Telegram User ID bhejo:", "user_id"), ("Username bhejo (optional) — ya 'skip' likho:", "username")],
    "maxtasks": [("⚡ Max concurrent tasks bhejo (1–50):", "value")],
}
INT_KEYS = {"source", "target", "user_id", "value"}
FLOW_BACK = {
    "addpair": "pairs", "setfilter": "pairs", "removesource": "pairs",
    "addword": "reps", "addlink": "reps", "block": "reps",
    "addadmin": "adm", "maxtasks": "set",
}
SKIP_WORDS = {"skip", "none", "-"}


async def finish_flow(action, data, uid):
    if action == "addpair":
        filt = data["filter"]
        filt = None if filt.lower() in SKIP_WORDS else filt
        return await act_addpair(data["source"], data["target"], filt)
    if action == "setfilter":
        return await act_setfilter(data["source"], data["target"], data["filter"])
    if action == "removesource":
        return await act_removepair(data["source"])
    if action == "addword":
        return await act_addword(data["old"], data["new"])
    if action == "addlink":
        return await act_addlink(data["old"], data["new"])
    if action == "block":
        return await act_block(data["text"])
    if action == "addadmin":
        name = data["username"]
        return await act_addadmin(uid, data["user_id"], None if name.lower() in SKIP_WORDS else name)
    if action == "maxtasks":
        return await act_maxtasks(data["value"])
    return "❌ Unknown action"


async def start_flow(update, context, action, preset=None):
    uid = update.effective_user.id
    if action == "addadmin" and not await bot.is_owner(uid):
        await show(update, "➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗", kb([("🔙 Back", "m:adm")]))
        return
    context.user_data["flow"] = {"action": action, "step": 0, "data": dict(preset or {})}
    await show(update, FLOWS[action][0][0], CANCEL_KB)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    if not flow:
        return
    uid = update.effective_user.id
    if not await bot.is_admin(uid):
        return

    action, steps = flow["action"], FLOWS[flow["action"]]
    key = steps[flow["step"]][1]
    value = update.message.text.strip()

    if key in INT_KEYS:
        try:
            value = int(value)
        except ValueError:
            await update.message.reply_text("❌ Sirf number bhejo (e.g. -100123456789).", reply_markup=CANCEL_KB)
            return

    flow["data"][key] = value
    flow["step"] += 1
    if flow["step"] < len(steps):
        await update.message.reply_text(steps[flow["step"]][0], reply_markup=CANCEL_KB)
        return

    context.user_data.pop("flow", None)
    try:
        notice = await finish_flow(action, flow["data"], uid)
    except Exception as e:
        logger.error(f"Flow error: {e}")
        notice = f"❌ Error: {e}"
    text, markup = await MENUS[FLOW_BACK[action]](uid)
    await update.message.reply_text(clip(with_notice(notice, text)), reply_markup=markup)


# ----------------------------------------------------------------------------
# Inline button router
# ----------------------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not await bot.is_admin(uid):
        await query.answer("❌ You are not authorised", show_alert=True)
        return
    await query.answer()

    p = query.data.split(":")
    kind = p[0]
    try:
        # ---- menus / cancel ----
        if kind == "m":
            context.user_data.pop("flow", None)
            text, markup = await MENUS[p[1]](uid)
            await show(update, text, markup)

        elif kind == "c":
            context.user_data.pop("flow", None)
            text, markup = await render_main(uid)
            await show(update, with_notice("❎ Cancelled", text), markup)

        # ---- settings ----
        elif kind == "s":
            if p[1] == "t" and p[2] in TOGGLES:
                notice = await act_toggle(p[2])
                text, markup = await render_settings(uid)
                await show(update, with_notice(notice, text), markup)
            elif p[1] == "q":
                notice = await act_set_forwarding(not bot.forwarding_enabled)
                text, markup = await render_main(uid)
                await show(update, with_notice(notice, text), markup)
            elif p[1] == "mt":
                presets = [1, 5, 10, 20, 30, 50]
                await show(
                    update,
                    f"⚡ Max Concurrent Tasks\n\nAbhi: {bot.max_concurrent_tasks}\nHigh-volume channels ke liye value badhao.",
                    kb(
                        [(str(n), f"s:m:{n}") for n in presets[:3]],
                        [(str(n), f"s:m:{n}") for n in presets[3:]],
                        [("✏️ Custom", "f:maxtasks")],
                        [("🔙 Back", "m:set")],
                    ),
                )
            elif p[1] == "m":
                notice = await act_maxtasks(int(p[2]))
                text, markup = await render_settings(uid)
                await show(update, with_notice(notice, text), markup)

        # ---- channel pairs ----
        elif kind == "p":
            if p[1] == "list":
                text, markup = pairs_list_view()
                await show(update, text, markup)
            elif p[1] == "v":
                text, markup = pair_detail_view(int(p[2]), int(p[3]))
                await show(update, text, markup)
            elif p[1] == "c":
                src, tgt = int(p[2]), int(p[3])
                notice = await act_setfilter(src, tgt, "none")
                text, markup = pair_detail_view(src, tgt)
                await show(update, with_notice(notice, text), markup)
            elif p[1] == "r":
                src, tgt = int(p[2]), int(p[3])
                await show(
                    update,
                    f"⚠️ Pair remove karna hai?\n\nSource: {src}\nTarget: {tgt}",
                    kb([("✅ Yes, remove", f"p:rc:{src}:{tgt}"), ("❌ No", f"p:v:{src}:{tgt}")]),
                )
            elif p[1] == "rc":
                notice = await act_removepair(int(p[2]), int(p[3]))
                text, markup = pairs_list_view()
                await show(update, with_notice(notice, text), markup)

        # ---- replacements ----
        elif kind == "r":
            if p[1] == "view":
                await show(update, replacements_text(), kb([("🔙 Back", "m:reps")]))
            elif p[1] == "rmw":
                text, markup = await removal_list_view("w")
                await show(update, text, markup)
            elif p[1] == "rml":
                text, markup = await removal_list_view("l")
                await show(update, text, markup)
            elif p[1] in ("dw", "dl"):
                coll = db.edits if p[1] == "dw" else db.links
                result = await coll.delete_one({"_id": ObjectId(p[2])})
                await bot.refresh_rules()
                notice = "✅ Removed" if result.deleted_count else "❌ Not found"
                text, markup = await removal_list_view("w" if p[1] == "dw" else "l")
                await show(update, with_notice(notice, text), markup)

        # ---- admins ----
        elif kind == "a":
            if not await bot.is_owner(uid):
                await show(update, "➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗", kb([("🔙 Back", "m:adm")]))
            elif p[1] == "rm":
                text, markup = await admin_removal_view()
                await show(update, text, markup)
            elif p[1] == "d":
                notice = await act_removeadmin(uid, int(p[2]))
                text, markup = await render_admins(uid)
                await show(update, with_notice(notice, text), markup)

        # ---- guided flows ----
        elif kind == "f":
            preset = None
            if p[1] == "setfilter":
                preset = {"source": int(p[2]), "target": int(p[3])}
            await start_flow(update, context, p[1], preset)

        # ---- reset ----
        elif kind == "x":
            if not await bot.is_owner(uid):
                await show(update, "➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗", MENU_BTN)
            elif p[1] == "ask":
                await show(
                    update,
                    "⚠️ Pairs, replacements, settings aur message mapping sab delete ho jayega.\n\nPakka reset karna hai?",
                    kb([("✅ Yes, reset", "x:go"), ("❌ No", "m:main")]),
                )
            elif p[1] == "go":
                notice = await act_reset(uid)
                text, markup = await render_main(uid)
                await show(update, with_notice(notice, text), markup)

    except Exception as e:
        logger.error(f"Button error ({query.data}): {e}")
        await show(update, f"❌ Error: {e}", MENU_BTN)


# ----------------------------------------------------------------------------
# Slash commands (all original commands still work)
# ----------------------------------------------------------------------------
def admin_required(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await bot.is_admin(update.effective_user.id):
            await update.message.reply_text("➢𝑌𝑜𝑢 𝐴𝑟𝑒 𝑁𝑜𝑡 𝐴𝑢𝑡ℎ𝑜𝑟𝑖𝑠𝑒𝑑 𝑡𝑜 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝐶𝑜𝑚𝑚𝑎𝑛𝑑 🤓🤝")
            return
        return await func(update, context)
    return wrapper


def owner_required(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await bot.is_owner(update.effective_user.id):
            await update.message.reply_text("➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗")
            return
        return await func(update, context)
    return wrapper


async def reply(update, text, markup=MENU_BTN):
    await update.message.reply_text(clip(text), reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await bot.is_admin(update.effective_user.id):
        await update.message.reply_text("➢𝑊𝑎𝑛𝑡 𝑇𝑜 𝐴𝑐𝑐𝑒𝑠𝑠 𝑇ℎ𝑖𝑠 𝐵𝑜𝑡 𝐶𝑜𝑛𝑡𝑎𝑐𝑡 𝐵𝑜𝑡 𝐴𝑑𝑚𝑖𝑛 🎗")
        return
    context.user_data.pop("flow", None)
    text, markup = await render_main(update.effective_user.id)
    await reply(update, text, markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await bot.is_admin(update.effective_user.id):
        await update.message.reply_text("➢𝑊𝑎𝑛𝑡 𝑇𝑜 𝐴𝑐𝑐𝑒𝑠𝑠 𝑇ℎ𝑖𝑠 𝐵𝑜𝑡 𝐶𝑜𝑛𝑡𝑎𝑐𝑡 𝐵𝑜𝑡 𝐴𝑑𝑚𝑖𝑛 🎗")
        return
    await reply(update, HELP_TEXT)


@admin_required
async def cancel_command(update, context):
    context.user_data.pop("flow", None)
    await reply(update, "❎ Cancelled")


@admin_required
async def cmd_addpair(update, context):
    if len(context.args) < 2:
        await reply(update, "➢ Format: /addpair source_channel_id target_channel_id [filter_keywords]\n"
                            "📝 Example: /addpair -100123456789 -100987654321 keyword1,keyword2")
        return
    try:
        source_id, target_id = int(context.args[0]), int(context.args[1])
    except ValueError:
        await reply(update, "❌ Channel IDs numbers hone chahiye")
        return
    filt = " ".join(context.args[2:]) or None
    await reply(update, await act_addpair(source_id, target_id, filt))


@admin_required
async def cmd_removepair(update, context):
    if not context.args:
        await reply(update, "➢ Format: /removepair source_channel_id [target_channel_id]")
        return
    try:
        source_id = int(context.args[0])
        target_id = int(context.args[1]) if len(context.args) > 1 else None
    except ValueError:
        await reply(update, "❌ Channel IDs numbers hone chahiye")
        return
    await reply(update, await act_removepair(source_id, target_id))


@admin_required
async def cmd_setfilter(update, context):
    if len(context.args) < 2:
        await reply(update, "➢ Format: /setfilter source_channel_id target_channel_id filter_keywords\n"
                            "📝 Example: /setfilter -100123456789 -100987654321 keyword1,keyword2\n"
                            "📝 To remove filter: /setfilter source_channel_id target_channel_id none")
        return
    try:
        source_id, target_id = int(context.args[0]), int(context.args[1])
    except ValueError:
        await reply(update, "❌ Channel IDs numbers hone chahiye")
        return
    filt = " ".join(context.args[2:]) or None
    await reply(update, await act_setfilter(source_id, target_id, filt))


@admin_required
async def cmd_listpairs(update, context):
    text, markup = pairs_list_view()
    await reply(update, text, markup)


@admin_required
async def cmd_addword(update, context):
    parts = update.message.text.split(None, 1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    for sep in ("=>", "|", "/"):          # '/' kept for backward compatibility
        if sep in raw:
            old, new = raw.split(sep, 1)
            await reply(update, await act_addword(old, new))
            return
    await reply(update, "↳ Format: /addword old_text=>new_text\n(ya old_text|new_text)")


@admin_required
async def cmd_addlink(update, context):
    parts = update.message.text.split(None, 2)
    if len(parts) < 3:
        await reply(update, "↳ Format: /addlink old_link new_link")
        return
    await reply(update, await act_addlink(parts[1], parts[2]))


@admin_required
async def cmd_removeword(update, context):
    if not context.args:
        await reply(update, "➢ Format: /removeword old_text")
        return
    await reply(update, await act_removeword(" ".join(context.args)))


@admin_required
async def cmd_removelink(update, context):
    if not context.args:
        await reply(update, "➢ Format: /removelink old_link")
        return
    await reply(update, await act_removelink(" ".join(context.args)))


@admin_required
async def cmd_showreps(update, context):
    await reply(update, replacements_text())


@admin_required
async def cmd_block(update, context):
    if not context.args:
        await reply(update, "➢ Format: /block text_to_block")
        return
    await reply(update, await act_block(" ".join(context.args)))


@admin_required
async def cmd_forward(update, context):
    if not context.args:
        await reply(update, f"📊 Forwarding Status: {on_off(bot.forwarding_enabled)}")
        return
    status = context.args[0].lower()
    if status not in ("on", "off"):
        await reply(update, "➢ Format: /forward on or /forward off")
        return
    await reply(update, await act_set_forwarding(status == "on"))


@admin_required
async def cmd_stop(update, context):
    await bot.set_setting("forwarding_enabled", "false")
    await reply(update, "🛑 Forwarding stopped")


@admin_required
async def cmd_editsync(update, context):
    await reply(update, await act_toggle("edit_sync"))


@admin_required
async def cmd_deletesync(update, context):
    await reply(update, await act_toggle("delete_sync"))


@admin_required
async def cmd_textonly(update, context):
    await reply(update, await act_toggle("text_only"))


@admin_required
async def cmd_settings(update, context):
    text, markup = await render_settings(update.effective_user.id)
    await reply(update, text, markup)


@admin_required
async def cmd_maxtasks(update, context):
    if not context.args:
        await reply(update, f"Current max concurrent tasks: {bot.max_concurrent_tasks}")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await reply(update, "❌ Number bhejo (1-50)")
        return
    await reply(update, await act_maxtasks(value))


@owner_required
async def cmd_reset(update, context):
    await reply(update, await act_reset(update.effective_user.id))


@owner_required
async def cmd_addadmin(update, context):
    if not context.args:
        await reply(update, "➢ Format: /addadmin user_id [username]")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await reply(update, "❌ user_id number hona chahiye")
        return
    username = context.args[1] if len(context.args) > 1 else None
    await reply(update, await act_addadmin(update.effective_user.id, user_id, username))


@owner_required
async def cmd_removeadmin(update, context):
    if not context.args:
        await reply(update, "➢ Format: /removeadmin user_id")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await reply(update, "❌ user_id number hona chahiye")
        return
    await reply(update, await act_removeadmin(update.effective_user.id, user_id))


@admin_required
async def cmd_listadmins(update, context):
    await reply(update, await admins_text())


async def error_handler(update, context):
    logger.error("Unhandled bot error", exc_info=context.error)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
async def main():
    await init_db()
    await bot.initialize()

    await client.start()
    me = await client.get_me()
    bot.me_id = me.id

    application = Application.builder().token(TOKEN).build()
    commands = {
        "start": start, "menu": start, "help": help_command, "cancel": cancel_command,
        "addpair": cmd_addpair, "removepair": cmd_removepair, "setfilter": cmd_setfilter,
        "listpairs": cmd_listpairs, "addword": cmd_addword, "addlink": cmd_addlink,
        "removeword": cmd_removeword, "removelink": cmd_removelink, "showreps": cmd_showreps,
        "forward": cmd_forward, "editsync": cmd_editsync, "deletesync": cmd_deletesync,
        "textonly": cmd_textonly, "settings": cmd_settings, "reset": cmd_reset, "stop": cmd_stop,
        "block": cmd_block, "addadmin": cmd_addadmin, "removeadmin": cmd_removeadmin,
        "listadmins": cmd_listadmins, "maxtasks": cmd_maxtasks,
    }
    for name, handler in commands.items():
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    application.add_error_handler(error_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Open control panel"),
            BotCommand("menu", "Open control panel"),
            BotCommand("settings", "Show settings"),
            BotCommand("help", "Help & commands"),
            BotCommand("cancel", "Cancel current action"),
        ])
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")
    logger.info("Bot started successfully with polling!")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:   # e.g. Windows
            pass

    client_task = asyncio.create_task(client.run_until_disconnected())
    stop_task = asyncio.create_task(stop_event.wait())
    await asyncio.wait({client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    logger.info("Shutting down...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
