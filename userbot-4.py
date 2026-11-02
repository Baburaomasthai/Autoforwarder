from flask import Flask
import threading
import os
import time

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ 𝑇𝑟𝑦𝑖𝑛𝑔 𝑇𝑜 𝑇𝑎𝑐𝑘𝑙𝑒 𝑆𝑒𝑡𝑏𝑎𝑐𝑘 𝑇𝐺 - @MrJaggiX!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))  
    app.run(host="0.0.0.0", port=port)

# Flask ko background thread me start karo
threading.Thread(target=run_flask, daemon=True).start()

import os
from telethon.sessions import StringSession
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaWebPage, MessageEntityTextUrl
from telethon import types as tl_types
from telethon.helpers import add_surrogate, del_surrogate
import copy
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
import signal
import sys
import subprocess

api_id = int(os.getenv("API_ID")) 
api_hash = os.getenv("API_HASH")

# Session string from environment variable
session_str = os.getenv("SESSION_STRING") 
client = TelegramClient(StringSession(session_str), api_id, api_hash)

# Bot token
TOKEN = os.getenv("BOT_TOKEN")

# Your Telegram User ID (replace with your actual ID)
OWNER_USER_ID = int(os.getenv("ADMIN_USER_ID"))

# MongoDB connection string
MONGODB_URL = os.getenv("MONGODB_URL")  # Render/host env variable me set karo

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# httpx INFO logs me bot token URL ke saath print ho jata hai, isliye isko chup karo
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_real_media(message):
    """Link preview (WebPage) ko media nahi maanta, sirf asli photo/video/file return karta hai"""
    m = message.media
    if m is None or isinstance(m, MessageMediaWebPage):
        return None
    return m

# Thread pool for database operations
db_executor = ThreadPoolExecutor(max_workers=5)

# MongoDB client
mongo_client = AsyncIOMotorClient(MONGODB_URL)
db = mongo_client.auto_forward_bot

# Global variable to track if bot is running
is_running = False

# DB setup
async def init_db():
    try:
        # Create collections if they don't exist
        collections = ['channel_pairs', 'edits', 'links', 'settings', 'admins', 'message_mapping']
        
        for collection_name in collections:
            if collection_name not in await db.list_collection_names():
                await db.create_collection(collection_name)
        
        # Create indexes for better performance
        await db.channel_pairs.create_index([("source_id", ASCENDING), ("target_id", ASCENDING)], unique=True)
        await db.message_mapping.create_index([("source_channel_id", ASCENDING), ("source_message_id", ASCENDING)])
        await db.settings.create_index("name", unique=True)
        await db.admins.create_index("user_id", unique=True)
        
        # Default settings
        default_settings = [
            {"name": "forwarding_enabled", "value": "true"},
            {"name": "edit_sync", "value": "false"},
            {"name": "delete_sync", "value": "false"},
            {"name": "text_only", "value": "false"},
            {"name": "max_concurrent_tasks", "value": "10"}
        ]
        
        for setting in default_settings:
            await db.settings.update_one(
                {"name": setting["name"]},
                {"$setOnInsert": setting},
                upsert=True
            )
        
        # Add yourself as the owner
        await db.admins.update_one(
            {"user_id": OWNER_USER_ID},
            {"$setOnInsert": {"user_id": OWNER_USER_ID, "username": "Owner", "is_owner": True}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

class ForwardBot:
    def __init__(self):
        self.channel_pairs = {}
        self.channel_filters = {}  # Store filter keywords for each channel pair
        self.message_mapping = defaultdict(dict)  # source_channel_id -> {source_message_id: {target_channel_id: target_message_id}}
        self.semaphore = None
        self.max_concurrent_tasks = 10
        self.last_processed_message = {}  # Track last processed message per channel
        self.processing_tasks = set()  # Track currently processing tasks
        self._initialized = False
        # Defaults (DB load fail ho to bhi attribute errors na aaye)
        self.forwarding_enabled = True
        self.edit_sync = False
        self.delete_sync = False
        self.text_only = False
        
    async def initialize(self):
        """Initialize bot data asynchronously"""
        if self._initialized:
            return
            
        await self.load_settings()
        await self.load_channel_pairs()
        await self.load_message_mapping()
        self._initialized = True
        
    async def load_channel_pairs(self):
        try:
            cursor = db.channel_pairs.find({})
            async for doc in cursor:
                source_id = doc['source_id']
                target_id = doc['target_id']
                filter_keywords = doc.get('filter_keywords', None)
                
                if source_id not in self.channel_pairs:
                    self.channel_pairs[source_id] = []
                    self.channel_filters[source_id] = {}
                    
                self.channel_pairs[source_id].append(target_id)
                self.channel_filters[source_id][target_id] = filter_keywords
        except Exception as e:
            logger.error(f"Error loading channel pairs: {e}")
            
    async def load_message_mapping(self):
        try:
            cursor = db.message_mapping.find({})
            async for doc in cursor:
                source_msg_id = doc['source_message_id']
                source_chan_id = doc['source_channel_id']
                target_msg_id = doc['target_message_id']
                target_chan_id = doc['target_channel_id']
                
                if source_chan_id not in self.message_mapping:
                    self.message_mapping[source_chan_id] = {}
                
                if source_msg_id not in self.message_mapping[source_chan_id]:
                    self.message_mapping[source_chan_id][source_msg_id] = {}
                    
                self.message_mapping[source_chan_id][source_msg_id][target_chan_id] = target_msg_id
        except Exception as e:
            logger.error(f"Error loading message mapping: {e}")
            
    async def save_message_mapping(self, source_message_id, source_channel_id, target_message_id, target_channel_id):
        try:
            await db.message_mapping.update_one(
                {
                    "source_message_id": source_message_id,
                    "source_channel_id": source_channel_id,
                    "target_channel_id": target_channel_id
                },
                {
                    "$set": {
                        "target_message_id": target_message_id,
                        "timestamp": asyncio.get_event_loop().time()
                    }
                },
                upsert=True
            )
            
            if source_channel_id not in self.message_mapping:
                self.message_mapping[source_channel_id] = {}
            
            if source_message_id not in self.message_mapping[source_channel_id]:
                self.message_mapping[source_channel_id][source_message_id] = {}
                
            self.message_mapping[source_channel_id][source_message_id][target_channel_id] = target_message_id
            
            return True
        except Exception as e:
            logger.error(f"Error saving message mapping: {e}")
            return False
        
    async def delete_message_mapping(self, source_message_id, source_channel_id, target_channel_id=None):
        try:
            query = {
                "source_message_id": source_message_id,
                "source_channel_id": source_channel_id
            }
            
            if target_channel_id:
                query["target_channel_id"] = target_channel_id
            
            await db.message_mapping.delete_many(query)
            
            if source_channel_id in self.message_mapping and source_message_id in self.message_mapping[source_channel_id]:
                if target_channel_id:
                    if target_channel_id in self.message_mapping[source_channel_id][source_message_id]:
                        del self.message_mapping[source_channel_id][source_message_id][target_channel_id]
                        if not self.message_mapping[source_channel_id][source_message_id]:
                            del self.message_mapping[source_channel_id][source_message_id]
                else:
                    del self.message_mapping[source_channel_id][source_message_id]
        except Exception as e:
            logger.error(f"Error deleting message mapping: {e}")
                
    async def load_settings(self):
        try:
            cursor = db.settings.find({})
            async for doc in cursor:
                name = doc['name']
                value = doc['value']
                
                if name == 'forwarding_enabled':
                    self.forwarding_enabled = value == 'true'
                elif name == 'edit_sync':
                    self.edit_sync = value == 'true'
                elif name == 'delete_sync':
                    self.delete_sync = value == 'true'
                elif name == 'text_only':
                    self.text_only = value == 'true'
                elif name == 'max_concurrent_tasks':
                    self.max_concurrent_tasks = int(value)
            
            self.semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
        except Exception as e:
            logger.error(f"Error loading settings: {e}")

    async def add_channel_pair(self, source_id, target_id, filter_keywords=None):
        try:
            await db.channel_pairs.insert_one({
                "source_id": source_id,
                "target_id": target_id,
                "filter_keywords": filter_keywords
            })
            
            if source_id not in self.channel_pairs:
                self.channel_pairs[source_id] = []
                self.channel_filters[source_id] = {}
                
            self.channel_pairs[source_id].append(target_id)
            self.channel_filters[source_id][target_id] = filter_keywords
            
            return True
        except Exception as e:
            logger.error(f"Error adding channel pair: {e}")
            return False
        
    async def update_channel_filter(self, source_id, target_id, filter_keywords):
        try:
            await db.channel_pairs.update_one(
                {"source_id": source_id, "target_id": target_id},
                {"$set": {"filter_keywords": filter_keywords}}
            )
            
            if source_id in self.channel_filters and target_id in self.channel_filters[source_id]:
                self.channel_filters[source_id][target_id] = filter_keywords
                
            return True
        except Exception as e:
            logger.error(f"Error updating filter: {e}")
            return False
        
    async def remove_channel_pair(self, source_id, target_id=None):
        try:
            query = {"source_id": source_id}
            if target_id:
                query["target_id"] = target_id
            
            await db.channel_pairs.delete_many(query)
            
            if source_id in self.channel_pairs:
                if target_id:
                    if target_id in self.channel_pairs[source_id]:
                        self.channel_pairs[source_id].remove(target_id)
                        if not self.channel_pairs[source_id]:
                            del self.channel_pairs[source_id]
                            
                    if source_id in self.channel_filters and target_id in self.channel_filters[source_id]:
                        del self.channel_filters[source_id][target_id]
                        if not self.channel_filters[source_id]:
                            del self.channel_filters[source_id]
                else:
                    del self.channel_pairs[source_id]
                    if source_id in self.channel_filters:
                        del self.channel_filters[source_id]
        except Exception as e:
            logger.error(f"Error removing channel pair: {e}")
            
    def get_all_channel_pairs(self):
        pairs = []
        for source_id, target_ids in self.channel_pairs.items():
            for target_id in target_ids:
                filter_keywords = self.channel_filters.get(source_id, {}).get(target_id, None)
                pairs.append((source_id, target_id, filter_keywords))
        return pairs
        
    async def set_setting(self, name, value):
        try:
            await db.settings.update_one(
                {"name": name},
                {"$set": {"value": value}},
                upsert=True
            )
            
            if name == 'forwarding_enabled':
                self.forwarding_enabled = value == 'true'
            elif name == 'edit_sync':
                self.edit_sync = value == 'true'
            elif name == 'delete_sync':
                self.delete_sync = value == 'true'
            elif name == 'text_only':
                self.text_only = value == 'true'
            elif name == 'max_concurrent_tasks':
                self.max_concurrent_tasks = int(value)
                self.semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
        except Exception as e:
            logger.error(f"Error setting setting: {e}")
            
    async def is_admin(self, user_id):
        try:
            doc = await db.admins.find_one({"user_id": user_id})
            return doc is not None
        except Exception as e:
            logger.error(f"Error checking admin: {e}")
            return False
        
    async def is_owner(self, user_id):
        try:
            doc = await db.admins.find_one({"user_id": user_id, "is_owner": True})
            return doc is not None
        except Exception as e:
            logger.error(f"Error checking owner: {e}")
            return False
        
    async def add_admin(self, user_id, username):
        try:
            await db.admins.insert_one({
                "user_id": user_id,
                "username": username,
                "is_owner": False
            })
            return True
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
            return False
        
    async def remove_admin(self, user_id):
        if await self.is_owner(user_id):
            return False
            
        try:
            await db.admins.delete_one({"user_id": user_id})
            return True
        except Exception as e:
            logger.error(f"Error removing admin: {e}")
            return False
        
    async def get_all_admins(self):
        try:
            cursor = db.admins.find({})
            admins = []
            async for doc in cursor:
                admins.append((doc['user_id'], doc['username'], doc.get('is_owner', False)))
            return admins
        except Exception as e:
            logger.error(f"Error getting admins: {e}")
            return []
        
    def should_forward_message(self, message_text, source_id, target_id):
        # If no filter is set, forward all messages
        if source_id not in self.channel_filters or target_id not in self.channel_filters[source_id]:
            return True
            
        filter_keywords = self.channel_filters[source_id][target_id]
        if not filter_keywords or filter_keywords.strip() == "":
            return True
            
        # Check if message contains any of the filter keywords
        keywords = [kw.strip() for kw in filter_keywords.split(",") if kw.strip()]
        message_lower = message_text.lower() if message_text else ""
        
        for keyword in keywords:
            if keyword.lower() in message_lower:
                return True
                
        return False

    async def get_edits_and_links(self):
        try:
            edits_cursor = db.edits.find({})
            links_cursor = db.links.find({})
            
            edits = []
            links = []
            
            async for doc in edits_cursor:
                edits.append((doc['old_text'], doc['new_text']))
                
            async for doc in links_cursor:
                links.append((doc['old_link'], doc['new_link']))
                
            return edits, links
        except Exception as e:
            logger.error(f"Error getting edits and links: {e}")
            return [], []

bot = ForwardBot()

def normalize_text(text):
    """Case aur extra spaces/newlines ignore karke compare karne ke liye"""
    return " ".join((text or "").split()).lower()

def build_block_pattern(blocked_text):
    """Blocked text ka regex: case-insensitive, aur spaces/newlines kuch bhi ho chalega"""
    tokens = (blocked_text or "").split()
    if not tokens:
        return None
    return re.compile(r"\s+".join(re.escape(t) for t in tokens), re.IGNORECASE)

def apply_text_rules(text, edits, links):
    """Blocks (text hata do) + word replacements + link replacements apply karta hai"""
    if not text:
        return text

    result = text
    removed_something = False
    normal_edits = []

    # 1) Pehle blocked text hatao (new_text khali = blocked)
    for old_text, new_text in edits:
        if new_text == "":
            pattern = build_block_pattern(old_text)
            if pattern:
                result, count = pattern.subn("", result)
                if count:
                    removed_something = True
        else:
            normal_edits.append((old_text, new_text))

    # 2) Phir normal word aur link replacements
    for old_text, new_text in normal_edits:
        result = result.replace(old_text, new_text)
    for old_link, new_link in links:
        result = result.replace(old_link, new_link)

    # 3) Text hatne ke baad bacha hua kachra saaf karo
    if removed_something:
        result = re.sub(r"(\*\*|__|~~|\|\|)\s*\1", "", result)  # khali bold/italic markers
        result = re.sub(r"[ \t]+\n", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        result = result.strip()

    return result

# ---------------------------------------------------------------------------
# Entity-aware text processing
# Telegram formatting (bold, hidden links, custom emoji, quote, spoiler...) ko
# markdown mein badalne ke bajaye original entities ke saath hi forward karte hain.
# ---------------------------------------------------------------------------

def _apply_matches(text, entities, matches):
    """text mein (start, end, new_text) matches apply karo aur entities ke offsets adjust karo"""
    if not matches:
        return text, entities

    matches = sorted(matches)
    parts, last = [], 0
    for start, end, new in matches:
        parts.append(text[last:start])
        parts.append(new)
        last = end
    parts.append(text[last:])
    new_text = "".join(parts)

    def map_pos(pos, is_end):
        delta = 0
        for start, end, new in matches:
            if end <= pos:
                delta += len(new) - (end - start)
            elif start < pos < end:
                return start + delta + (len(new) if is_end else 0)
            else:
                break
        return pos + delta

    new_entities = []
    for ent in entities:
        new_start = map_pos(ent.offset, False)
        new_end = map_pos(ent.offset + ent.length, True)
        if new_end <= new_start:
            continue
        ent.offset = new_start
        ent.length = new_end - new_start
        new_entities.append(ent)

    return new_text, new_entities

def apply_rules_with_entities(text, entities, edits, links):
    """Blocks + replacements + link replacements apply karta hai aur (text, entities) return karta hai.
    Formatting entities (bold, hidden links, custom emoji, quote...) sahi jagah par bani rehti hain."""
    if not text:
        return text, list(entities or [])

    ents = [copy.copy(e) for e in (entities or [])]
    # Mention-by-id entities ko dobara bheja nahi ja sakta
    ents = [e for e in ents if not isinstance(e, tl_types.MessageEntityMentionName)]

    blocked_texts = [old for old, new in edits if new == "" and (old or "").strip()]
    normal_rules = [(old, new) for old, new in edits if new != ""] + list(links)

    # Hidden links (jaise "Download" ke peeche chhupa URL) ko bhi handle karo
    url_patterns = [build_block_pattern(b) for b in blocked_texts]
    kept = []
    for ent in ents:
        if isinstance(ent, MessageEntityTextUrl):
            url = ent.url or ""
            if any(pat and pat.search(url) for pat in url_patterns):
                continue  # blocked link hata do, visible text rehne do
            for old, new in normal_rules:
                if old:
                    url = url.replace(old, new)
            ent.url = url
        kept.append(ent)
    ents = kept

    # UTF-16 offsets ke saath kaam karne ke liye surrogate form
    current = add_surrogate(text)

    removed_something = False
    for blocked in blocked_texts:
        pattern = build_block_pattern(add_surrogate(blocked))
        if not pattern:
            continue
        matches = [(m.start(), m.end(), "") for m in pattern.finditer(current)]
        if matches:
            removed_something = True
            current, ents = _apply_matches(current, ents, matches)

    for old, new in normal_rules:
        if not old:
            continue
        old_s, new_s = add_surrogate(old), add_surrogate(new)
        matches, idx = [], current.find(old_s)
        while idx != -1:
            matches.append((idx, idx + len(old_s), new_s))
            idx = current.find(old_s, idx + len(old_s))
        current, ents = _apply_matches(current, ents, matches)

    # Block ke baad bachi khali jagah saaf karo
    if removed_something:
        for cleanup in (r"[ \t]+(?=\n)", r"\n{3,}", r"\A\s+", r"\s+\Z"):
            matches = []
            for m in re.finditer(cleanup, current):
                replacement = "\n\n" if cleanup == r"\n{3,}" else ""
                matches.append((m.start(), m.end(), replacement))
            current, ents = _apply_matches(current, ents, matches)

    return del_surrogate(current), ents

def strip_premium_entities(entities):
    """Custom emoji / quote jaisi entities hata do (fallback ke liye)"""
    blocked_types = tuple(
        t for t in (
            getattr(tl_types, "MessageEntityCustomEmoji", None),
            getattr(tl_types, "MessageEntityBlockquote", None),
        ) if t is not None
    )
    return [e for e in entities if not (blocked_types and isinstance(e, blocked_types))]

async def send_long_message(update: Update, text: str, limit: int = 3800):
    """Telegram ki 4096 char limit se bachne ke liye lamba text parts mein bhejta hai"""
    chunk = ""
    for line in text.split("\n"):
        while len(line) > limit:  # bahut lambi single line
            if chunk:
                await update.message.reply_text(chunk)
                chunk = ""
            await update.message.reply_text(line[:limit])
            line = line[limit:]
        if len(chunk) + len(line) + 1 > limit:
            await update.message.reply_text(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk.strip():
        await update.message.reply_text(chunk)

def admin_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await bot.is_admin(user_id):
            await update.message.reply_text("➢𝑌𝑜𝑢 𝐴𝑟𝑒 𝑁𝑜𝑡 𝐴𝑢𝑡ℎ𝑜𝑟𝑖𝑠𝑒𝑑 𝑡𝑜 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝐶𝑜𝑚𝑚𝑎𝑛𝑑 🤓🤝")
            return
        return await func(update, context)
    return wrapper

def owner_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await bot.is_owner(user_id):
            await update.message.reply_text("➢𝑂𝑛𝑙𝑦 𝑡ℎ𝑒 𝑏𝑜𝑡 𝑜𝑤𝑛𝑒𝑟 𝑐𝑎𝑛 𝑢𝑠𝑒 𝑡ℎ𝑖𝑠 𝑐𝑜𝑚𝑚𝑎𝑛𝑑 🎗")
            return
        return await func(update, context)
    return wrapper

@admin_required
async def add_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message_text = update.message.text
        command_text = message_text.replace('/add_edit', '').replace('/addword', '').strip()
        
        if '/' not in command_text:
            await update.message.reply_text("↳ Format: /addword old_text/new_text")
            return
            
        old_text, new_text = command_text.split('/', 1)
        old_text = old_text.strip()
        new_text = new_text.strip()
        
        if not old_text or not new_text:
            await update.message.reply_text("↳ Both old_text and new_text required")
            return
        
        await db.edits.insert_one({
            "old_text": old_text,
            "new_text": new_text
        })
        
        await update.message.reply_text(f"✅ Replacement added:\n{old_text} → {new_text}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message_text = update.message.text
        command_text = message_text.replace('/addlink', '').strip()
        
        if ' ' in command_text:
            parts = command_text.split(' ', 1)
            old_link = parts[0].strip()
            new_link = parts[1].strip()
        else:
            await update.message.reply_text("↳ Format: /addlink old_link new_link")
            return
        
        if not old_link or not new_link:
            await update.message.reply_text("❌ Both links required")
            return
        
        await db.links.insert_one({
            "old_link": old_link,
            "new_link": new_link
        })
        
        await update.message.reply_text(f"✅ Link replacement added:\n{old_link} → {new_link}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_edit(update: Update,context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /removeword old_text")
            return

        old_text = ' '.join(context.args)
        await db.edits.delete_one({"old_text": old_text})
        await update.message.reply_text(f"✅ Replacement removed: {old_text}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /removelink old_link")
            return

        old_link = ' '.join(context.args)
        await db.links.delete_one({"old_link": old_link})
        await update.message.reply_text(f"✅ Link replacement removed: {old_link}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def show_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        replacements = []
        blocked = []
        links = []

        async for doc in db.edits.find({}):
            old_text = doc.get("old_text", "")
            new_text = doc.get("new_text", "")
            if new_text == "":
                blocked.append(old_text)
            else:
                replacements.append((old_text, new_text))

        async for doc in db.links.find({}):
            links.append((doc.get("old_link", ""), doc.get("new_link", "")))

        if not replacements and not blocked and not links:
            await update.message.reply_text("❌ No active replacements found")
            return

        response = "📜 Active Replacements:\n"

        if replacements:
            response += "\n📝 Text Replacements:\n"
            response += "\n".join(f"{i}. {o} → {n}" for i, (o, n) in enumerate(replacements, 1)) + "\n"

        if links:
            response += "\n🔗 Link Replacements:\n"
            response += "\n".join(f"{i}. {o} → {n}" for i, (o, n) in enumerate(links, 1)) + "\n"

        if blocked:
            response += "\n🚫 Blocked Content (use /unblock text):\n"
            response += "\n".join(f"{i}. {b}" for i, b in enumerate(blocked, 1)) + "\n"

        await send_long_message(update, response)

    except Exception as e:
        logger.error(f"Error in show_edits: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def forward_on_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            status = "ON ✅" if bot.forwarding_enabled else "OFF ❌"
            await update.message.reply_text(f"📊 Forwarding Status: {status}")
            return
            
        status = context.args[0].lower()
        if status not in ['on', 'off']:
            await update.message.reply_text("➢ Format: /forward on or /forward off")
            return
            
        new_status = status == 'on'
        await bot.set_setting('forwarding_enabled', 'true' if new_status else 'false')
        
        status_text = "ON ✅" if new_status else "OFF ❌"
        await update.message.reply_text(f"✅ Forwarding turned {status_text}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bot.set_setting('forwarding_enabled', 'false')
    await update.message.reply_text("🛑 Forwarding stopped")

@admin_required
async def check_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "ON ✅" if bot.forwarding_enabled else "OFF ❌"
    edit_status = "ON ✅" if bot.edit_sync else "OFF ❌"
    delete_status = "ON ✅" if bot.delete_sync else "OFF ❌"
    text_only_status = "ON ✅" if bot.text_only else "OFF ❌"
    
    channel_pairs = bot.get_all_channel_pairs()
    if channel_pairs:
        channels_text = "\n".join([f"Source: {src} → Target: {tgt} | Filter: {filt if filt else 'None'}" for src, tgt, filt in channel_pairs])
        await update.message.reply_text(
            f"⚙️ Settings:\n"
            f"Forwarding: {status}\n"
            f"Edit Sync: {edit_status}\n"
            f"Delete Sync: {delete_status}\n"
            f"Text Only Mode: {text_only_status}\n"
            f"Max Concurrent Tasks: {bot.max_concurrent_tasks}\n\n"
            f"📡 Channel Pairs:\n{channels_text}"
        )
    else:
        await update.message.reply_text("No channel pairs set up yet")

async def perform_reset():
    """DB aur memory dono ko poori tarah reset karta hai (admins delete nahi hote)"""
    try:
        for name in ("channel_pairs", "edits", "links", "settings", "message_mapping"):
            await db[name].delete_many({})

        # In-memory cache bhi clear karo, warna purane pairs forward hote rahenge
        bot.channel_pairs.clear()
        bot.channel_filters.clear()
        bot.message_mapping.clear()
        bot.last_processed_message.clear()

        default_settings = [
            {"name": "forwarding_enabled", "value": "true"},
            {"name": "edit_sync", "value": "false"},
            {"name": "delete_sync", "value": "false"},
            {"name": "text_only", "value": "false"},
            {"name": "max_concurrent_tasks", "value": "10"}
        ]
        for setting in default_settings:
            await db.settings.update_one({"name": setting["name"]}, {"$set": setting}, upsert=True)

        await bot.load_settings()
        return True
    except Exception as e:
        logger.error(f"Error resetting: {e}")
        return False

@owner_required
async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Reset Everything", callback_data="reset_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
    ]])
    await update.message.reply_text(
        "⚠️ Do you really want to reset?\n\n"
        "Ye sab delete ho jayega:\n"
        "• Saare channel pairs & filters\n"
        "• Text/link replacements & blocked content\n"
        "• Settings (default pe wapas)\n"
        "• Message mapping (edit/delete/reply sync history)\n\n"
        "Admins delete nahi honge. Ye action undo nahi ho sakta.",
        reply_markup=keyboard
    )

async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await bot.is_owner(query.from_user.id):
        await query.answer("Only the bot owner can do this.", show_alert=True)
        return

    await query.answer()

    if query.data == "reset_cancel":
        await query.edit_message_text("❎ Reset cancelled. Kuch delete nahi hua.")
        return

    if query.data == "reset_confirm":
        if await perform_reset():
            await query.edit_message_text("✅ All settings reset successfully")
        else:
            await query.edit_message_text("❌ Reset failed. Logs check karo.")

@admin_required
async def add_channel_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("➢ Format: /addpair source_channel_id target_channel_id [filter_keywords]")
            await update.message.reply_text("📝 Example: /addpair -100123456789 -100987654321 keyword1,keyword2")
            return
            
        source_id = int(context.args[0])
        target_id = int(context.args[1])
        
        # Check if filter keywords are provided
        filter_keywords = ' '.join(context.args[2:]) if len(context.args) > 2 else None
        
        success = await bot.add_channel_pair(source_id, target_id, filter_keywords)
        if success:
            if filter_keywords:
                await update.message.reply_text(f"✅ Channel pair added with filter:\nSource: {source_id} → Target: {target_id}\nFilter: {filter_keywords}")
            else:
                await update.message.reply_text(f"✅ Channel pair added:\nSource: {source_id} → Target: {target_id}")
        else:
            await update.message.reply_text("😃 This channel pair already exists")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def set_channel_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("➢ Format: /setfilter source_channel_id target_channel_id filter_keywords")
            await update.message.reply_text("📝 Example: /setfilter -100123456789 -100987654321 keyword1,keyword2,keyword3")
            await update.message.reply_text("📝 To remove filter: /setfilter source_channel_id target_channel_id none")
            return
            
        source_id = int(context.args[0])
        target_id = int(context.args[1])
        
        # Check if the channel pair exists
        if source_id not in bot.channel_pairs or target_id not in bot.channel_pairs[source_id]:
            await update.message.reply_text("🥱 Channel pair does not exist")
            return
            
        # Get filter keywords
        filter_keywords = ' '.join(context.args[2:]) if len(context.args) > 2 else None
        
        # Handle "none" keyword to remove filter
        if filter_keywords and filter_keywords.lower() == "none":
            filter_keywords = None
            
        success = await bot.update_channel_filter(source_id, target_id, filter_keywords)
        if success:
            if filter_keywords:
                await update.message.reply_text(f"✅ Filter updated:\nSource: {source_id} → Target: {target_id}\nFilter: {filter_keywords}")
            else:
                await update.message.reply_text(f"✅ Filter removed:\nSource: {source_id} → Target: {target_id}")
        else:
            await update.message.reply_text("🥲 Error updating filter")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_channel_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /removepair source_channel_id [target_channel_id]")
            return
            
        source_id = int(context.args[0])
        target_id = int(context.args[1]) if len(context.args) > 1 else None
        
        await bot.remove_channel_pair(source_id, target_id)
        
        if target_id:
            await update.message.reply_text(f"✅ Channel pair removed:\nSource: {source_id} → Target: {target_id}")
        else:
            await update.message.reply_text(f"✅ All channel pairs removed for source: {source_id}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def list_channel_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_pairs = bot.get_all_channel_pairs()
    if channel_pairs:
        response = "📡 Channel Pairs:\n\n" + "\n".join([f"Source: {src} → Target: {tgt} | Filter: {filt if filt else 'None'}" for src, tgt, filt in channel_pairs])
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("🥱 No channel pairs set up yet")

def get_command_argument_text(update: Update):
    """Command ke baad ka poora text (newlines ke saath) return karta hai"""
    parts = (update.message.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""

@admin_required
async def block_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text_to_block = get_command_argument_text(update)
        if not text_to_block:
            await update.message.reply_text("➢ Format: /block text_to_block")
            return

        # Pehle se blocked hai to dobara mat daalo
        async for doc in db.edits.find({"new_text": ""}):
            if normalize_text(doc.get("old_text")) == normalize_text(text_to_block):
                await update.message.reply_text("🤔 Ye text pehle se blocked hai")
                return

        await db.edits.insert_one({
            "old_text": text_to_block,
            "new_text": ""
        })
        
        await update.message.reply_text(
            f"✅ Content blocked: {text_to_block}\n\n"
            f"Ab jis bhi message mein ye text hoga (capital/small kuch bhi), wo hissa hata ke forward hoga."
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def unblock_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text_to_unblock = get_command_argument_text(update)
        if not text_to_unblock:
            await update.message.reply_text("➢ Format: /unblock text_to_unblock\n➢ Sab unblock karne ke liye: /unblock all\n➢ Blocked list dekhne ke liye: /showreps")
            return

        if text_to_unblock.lower() == "all":
            result = await db.edits.delete_many({"new_text": ""})
            await update.message.reply_text(f"✅ Saare blocked content unblock ho gaye ({result.deleted_count})")
            return

        target = normalize_text(text_to_unblock)
        ids_to_delete = []
        async for doc in db.edits.find({"new_text": ""}):
            if normalize_text(doc.get("old_text")) == target:
                ids_to_delete.append(doc["_id"])

        if ids_to_delete:
            await db.edits.delete_many({"_id": {"$in": ids_to_delete}})
            await update.message.reply_text(f"✅ Content unblocked: {text_to_unblock}")
        else:
            await update.message.reply_text("🤔 Ye text blocked list mein nahi mila. /showreps se check karo.")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def toggle_edit_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_status = not bot.edit_sync
    await bot.set_setting('edit_sync', 'true' if new_status else 'false')
    
    status_text = "ON ✅" if new_status else "OFF ❌"
    await update.message.reply_text(f"✅ Edit Sync turned {status_text}")

@admin_required
async def toggle_delete_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_status = not bot.delete_sync
    await bot.set_setting('delete_sync', 'true' if new_status else 'false')
    
    status_text = "ON ✅" if new_status else "OFF ❌"
    await update.message.reply_text(f"✅ Delete Sync turned {status_text}")

@admin_required
async def toggle_text_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_status = not bot.text_only
    await bot.set_setting('text_only', 'true' if new_status else 'false')
    
    status_text = "ON ✅" if new_status else "OFF ❌"
    await update.message.reply_text(f"✅ Text Only Mode turned {status_text}\n\n"
                                   f"अब केवल text messages ही forward होंगे, media files नहीं।")

@owner_required
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /addadmin user_id [username]")
            return
            
        user_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else "Unknown"
        
        success = await bot.add_admin(user_id, username)
        if success:
            await update.message.reply_text(f"✅ Admin added: {user_id} ({username})")
        else:
            await update.message.reply_text("🤔 This user is already an admin")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@owner_required
async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /removeadmin user_id")
            return
            
        user_id = int(context.args[0])
        success = await bot.remove_admin(user_id)
        if success:
            await update.message.reply_text(f"✅ Admin removed: {user_id}")
        else:
            await update.message.reply_text("😎 Cannot remove the owner or user not found")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = await bot.get_all_admins()
    if admins:
        response = "👑 Admins:\n\n" + "\n".join([f"ID: {user_id}, Name: {username}{' (Owner)' if is_owner else ''}" for user_id, username, is_owner in admins])
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("🤔 No admins found")

@admin_required
async def set_concurrent_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text(f"Current max concurrent tasks: {bot.max_concurrent_tasks}")
            return
            
        max_tasks = int(context.args[0])
        if max_tasks < 1 or max_tasks > 50:
            await update.message.reply_text("Please enter a value between 1 and 50")
            return
            
        await bot.set_setting('max_concurrent_tasks', str(max_tasks))
        await update.message.reply_text(f"✅ Max concurrent tasks set to {max_tasks}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await bot.is_admin(user_id):
        await update.message.reply_text("➢𝑊𝑎𝑛𝑡 𝑇𝑜 𝐴𝑐𝑐𝑒𝑠𝑠 𝑇ℎ𝑖𝑠 𝐵𝑜𝑡 𝐶𝑜𝑛𝑡𝑎𝑐𝑡 𝐵𝑜𝑡 𝐴𝑑𝑚𝑖𝑛 🎗")
        return
        
    await update.message.reply_text(
        "🤖 Welcome to the Auto Forward Bot!\n\n"
        "📚 Available Commands:\n"
        "• /start - Show this message\n"
        "• /addpair - Add a channel pair\n"
        "• /removepair - Remove a channel pair\n"
        "• /setfilter - Set filter for a channel pair\n"
        "• /listpairs - List all channel pairs\n"
        "• /addword - Add text replacement\n"
        "• /addlink - Add link replacement\n"
        "• /removeword - Remove text replacement\n"
        "• /removelink - Remove link replacement\n"
        "• /showreps - Show all replacements\n"
        "• /forward - Toggle forwarding on/off\n"
        "• /editsync - Toggle edit sync\n"
        "• /deletesync - Toggle delete sync\n"
        "• /textonly - Toggle text only mode\n"
        "• /settings - Show current settings\n"
        "• /block - Block specific content\n"
        "• /unblock - Unblock blocked content (or /unblock all)\n"
        "• /addadmin - Add an admin (Owner only)\n"
        "• /removeadmin - Remove an admin (Owner only)\n"
        "• /listadmins - List all admins\n"
        "• /maxtasks - Set max concurrent tasks\n"
        "• /reset - Reset all settings (Owner only)\n"
        "• /stop - Stop forwarding\n\n"
        "📝 Use /help command for detailed instructions"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await bot.is_admin(user_id):
        await update.message.reply_text("➢𝑊𝑎𝑛𝑡 𝑇𝑜 𝐴𝑐𝑐𝑒𝑠𝑠 𝑇ℎ𝑖𝑠 𝐵𝑜𝑡 𝐶𝑜𝑛𝑡𝑎𝑐𝑡 𝐵𝑜𝑡 𝐴𝑑𝑚𝑖𝑛 🎗")
        return
        
    await update.message.reply_text(
        "🤖 Auto Forward Bot Help\n\n"
        "📚 Setup Instructions:\n"
        "1. Add user account as admin in target channels with necessary permissions\n"
        "2. Use /addpair source_channel_id target_channel_id to create a forwarding pair\n"
        "3. Use /setfilter to add keyword filtering (optional)\n"
        "4. Use /addword and /addlink to set up text/link replacements (optional)\n\n"
        "📝 Channel IDs:\n"
        "• Channel IDs are negative numbers (e.g., -100123456789)\n"
        "• To get a channel ID, forward a message from that channel to @userinfobot\n\n"
        "🔧 Advanced Features:\n"
        "• Use /editsync and /deletesync to enable edit and delete synchronization\n"
        "• Use /textonly to forward only text messages\n"
        "• Use /block and /unblock to hide or restore specific text\n"
        "• Replies source channel jaise hi target channel mein reply ban kar jate hain\n"
        "• Use /maxtasks to adjust performance for high-volume channels\n\n"
        "📊 Monitoring:\n"
        "• Use /settings to check current configuration\n"
        "• Use /listpairs to see all channel pairs\n"
        "• Use /showreps to see all text/link replacements"
    )

# Message processing functions
async def resolve_reply_target(source_id, target_id, message):
    """Agar source message kisi ka reply hai to target channel mein uska forwarded message ID dhundo"""
    reply_header = getattr(message, "reply_to", None)
    source_reply_id = getattr(reply_header, "reply_to_msg_id", None) if reply_header else None
    if not source_reply_id:
        return None

    # Jis message ka reply hai wo abhi forward ho raha ho to thoda wait karo
    for attempt in range(3):
        target_msg_id = bot.message_mapping.get(source_id, {}).get(source_reply_id, {}).get(target_id)
        if target_msg_id:
            return target_msg_id
        if attempt < 2:
            await asyncio.sleep(1.5)
    return None

async def process_message(event, source_id, target_id, message_text, media=None):
    """Process and forward a single message"""
    try:
        # Reply structure maintain karne ke liye target ka reply_to ID
        reply_to_id = await resolve_reply_target(source_id, target_id, event.message)

        async with bot.semaphore:
            # Check if we should forward this message based on filters
            if not bot.should_forward_message(message_text, source_id, target_id):
                logger.info(f"Skipping message due to filter: {source_id} -> {target_id}")
                return None

            # Apply text replacements
            edits, links = await bot.get_edits_and_links()
            processed_text, entities = apply_rules_with_entities(
                message_text, event.message.entities, edits, links
            )

            # Blocking ke baad kuch bacha hi nahi to empty message mat bhejo
            if not processed_text.strip() and not media:
                return None

            async def send(entities_to_use):
                if media and not bot.text_only:
                    return await client.send_file(
                        target_id, media, caption=processed_text,
                        formatting_entities=entities_to_use or None,
                        parse_mode=None, reply_to=reply_to_id
                    )
                return await client.send_message(
                    target_id, processed_text,
                    formatting_entities=entities_to_use or None,
                    parse_mode=None, link_preview=False, reply_to=reply_to_id
                )

            if bot.text_only and not processed_text:
                return None

            try:
                return await send(entities)
            except Exception as first_error:
                # Account Premium nahi ho to custom emoji/quote reject ho sakte hain, to unke bina bhejo
                logger.warning(f"Send with full formatting failed ({first_error}); retrying without premium entities")
                return await send(strip_premium_entities(entities))

    except Exception as e:
        logger.error(f"Error forwarding message: {e}")
        return None

@client.on(events.NewMessage)
async def handle_new_message(event):
    if not bot.forwarding_enabled:
        return
        
    try:
        source_id = event.chat_id
        if source_id not in bot.channel_pairs:
            return
            
        # Album (grouped media) ko Album handler sambhalta hai, warna har photo alag-alag message ban jati hai
        if event.message and event.message.grouped_id:
            return

        # Skip if it's our own message to prevent loops
        if event.message and event.message.sender_id == (await client.get_me()).id:
            return
            
        message_text = event.message.raw_text or ""
        media = get_real_media(event.message) if not bot.text_only else None
        
        # Create tasks for all target channels
        tasks = []
        for target_id in bot.channel_pairs[source_id]:
            task = asyncio.create_task(
                process_message_and_save_mapping(event, source_id, target_id, message_text, media)
            )
            tasks.append(task)
            
        # Wait for all tasks to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    except Exception as e:
        logger.error(f"Error in handle_new_message: {e}")

async def process_message_and_save_mapping(event, source_id, target_id, message_text, media):
    """Process a message and save the mapping between source and target messages"""
    try:
        sent_message = await process_message(event, source_id, target_id, message_text, media)
        if sent_message:
            await bot.save_message_mapping(
                event.message.id, source_id, 
                sent_message.id, target_id
            )
    except Exception as e:
        logger.error(f"Error processing message: {e}")

class FixedParseMode:
    """Telethon ko pehle se bane (text, entities) dene ke liye custom parse mode (album captions ke liye)"""
    def __init__(self, table):
        self.table = table

    def parse(self, text):
        return self.table.get(text, (text, []))

    def unparse(self, text, entities):
        return text

async def process_album(event, source_id, target_id):
    """Album ko ek hi album ki tarah forward karo. [(source_msg_id, target_msg_id), ...] return karta hai"""
    messages = sorted(event.messages, key=lambda m: m.id)
    reply_to_id = await resolve_reply_target(source_id, target_id, messages[0])
    edits, links = await bot.get_edits_and_links()

    async with bot.semaphore:
        combined_text = next((m.raw_text for m in messages if m.raw_text), "")
        if not bot.should_forward_message(combined_text, source_id, target_id):
            logger.info(f"Skipping album due to filter: {source_id} -> {target_id}")
            return []

        processed = []
        for m in messages:
            text, entities = apply_rules_with_entities(m.raw_text or "", m.entities, edits, links)
            processed.append((m, get_real_media(m), text, entities))

        media_items = [item for item in processed if item[1]]

        # Text-only mode ya media hi nahi: sirf caption text bhejo
        if bot.text_only or not media_items:
            first = next(((t, e) for _, _, t, e in processed if t.strip()), None)
            if not first:
                return []
            sent = await client.send_message(
                target_id, first[0], formatting_entities=first[1] or None,
                parse_mode=None, link_preview=False, reply_to=reply_to_id
            )
            return [(messages[0].id, sent.id)]

        async def send(strip_premium):
            table, captions = {}, []
            for i, (_, _, text, entities) in enumerate(media_items):
                use_entities = strip_premium_entities(entities) if strip_premium else entities
                if text.strip():
                    key = f"__album_caption_{i}__"
                    table[key] = (text, use_entities)
                    captions.append(key)
                else:
                    captions.append("")
            return await client.send_file(
                target_id, [item[1] for item in media_items],
                caption=captions, parse_mode=FixedParseMode(table), reply_to=reply_to_id
            )

        try:
            sent = await send(False)
        except Exception as first_error:
            logger.warning(f"Album send with full formatting failed ({first_error}); retrying without premium entities")
            sent = await send(True)

        if not isinstance(sent, (list, tuple)):
            sent = [sent]
        return [(item[0].id, msg.id) for item, msg in zip(media_items, sent)]

async def process_album_and_save_mapping(event, source_id, target_id):
    try:
        pairs = await process_album(event, source_id, target_id)
        for source_msg_id, target_msg_id in pairs:
            await bot.save_message_mapping(source_msg_id, source_id, target_msg_id, target_id)
    except Exception as e:
        logger.error(f"Error processing album: {e}")

@client.on(events.Album)
async def handle_album(event):
    if not bot.forwarding_enabled:
        return

    try:
        source_id = event.chat_id
        if source_id not in bot.channel_pairs:
            return

        first_message = event.messages[0]
        if first_message.sender_id == (await client.get_me()).id:
            return

        tasks = [
            asyncio.create_task(process_album_and_save_mapping(event, source_id, target_id))
            for target_id in bot.channel_pairs[source_id]
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        logger.error(f"Error in handle_album: {e}")

async def edit_target_message(chat_id, message_id, text, entities):
    """Target message edit karo (formatting ke saath, fail ho to premium entities ke bina)"""
    try:
        return await client.edit_message(
            chat_id, message_id, text,
            formatting_entities=entities or None, parse_mode=None, link_preview=False
        )
    except Exception as first_error:
        logger.warning(f"Edit with full formatting failed ({first_error}); retrying without premium entities")
        return await client.edit_message(
            chat_id, message_id, text,
            formatting_entities=strip_premium_entities(entities) or None,
            parse_mode=None, link_preview=False
        )

@client.on(events.MessageEdited)
async def handle_edit(event):
    if not bot.forwarding_enabled or not bot.edit_sync:
        return
        
    try:
        source_id = event.chat_id
        if source_id not in bot.message_mapping or event.message.id not in bot.message_mapping[source_id]:
            return
            
        message_text = event.message.raw_text or ""
        
        # Apply text replacements (formatting entities ke saath)
        edits, links = await bot.get_edits_and_links()
        processed_text, entities = apply_rules_with_entities(
            message_text, event.message.entities, edits, links
        )
        
        # Get all target messages that need to be updated
        target_messages = bot.message_mapping[source_id][event.message.id]
        
        # Create edit tasks
        tasks = []
        for target_channel_id, target_message_id in target_messages.items():
            task = asyncio.create_task(
                edit_target_message(target_channel_id, target_message_id, processed_text, entities)
            )
            tasks.append(task)
            
        # Execute all edits concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    except Exception as e:
        logger.error(f"Error in handle_edit: {e}")

@client.on(events.MessageDeleted)
async def handle_delete(event):
    if not bot.forwarding_enabled or not bot.delete_sync:
        return
        
    try:
        for deleted_msg in event.deleted_ids:
            source_id = event.chat_id
            if source_id in bot.message_mapping and deleted_msg in bot.message_mapping[source_id]:
                target_messages = bot.message_mapping[source_id][deleted_msg]
                
                # Create delete tasks
                tasks = []
                for target_channel_id, target_message_id in target_messages.items():
                    task = asyncio.create_task(
                        client.delete_messages(target_channel_id, target_message_id)
                    )
                    tasks.append(task)
                    
                # Execute all deletes concurrently
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                # Clean up the mapping
                await bot.delete_message_mapping(deleted_msg, source_id)
                
    except Exception as e:
        logger.error(f"Error in handle_delete: {e}")

async def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global is_running
    logger.info(f"Received signal {signum}, shutting down...")
    is_running = False
    # Give some time for cleanup
    await asyncio.sleep(2)
    sys.exit(0)

async def main():
    global is_running
    is_running = True
    
    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(
            sig, lambda sig=sig: asyncio.create_task(signal_handler(sig, None))
        )
    
    # Initialize database
    await init_db()
    
    # Initialize bot data
    await bot.initialize()
    
    # Start the Telegram client
    await client.start()
    
    # Set up the bot application
    application = Application.builder().token(TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addpair", add_channel_pair))
    application.add_handler(CommandHandler("removepair", remove_channel_pair))
    application.add_handler(CommandHandler("setfilter", set_channel_filter))
    application.add_handler(CommandHandler("listpairs", list_channel_pairs))
    application.add_handler(CommandHandler("addword", add_edit))
    application.add_handler(CommandHandler("addlink", add_link))
    application.add_handler(CommandHandler("removeword", remove_edit))
    application.add_handler(CommandHandler("removelink", remove_link))
    application.add_handler(CommandHandler("showreps", show_edits))
    application.add_handler(CommandHandler("forward", forward_on_off))
    application.add_handler(CommandHandler("editsync", toggle_edit_sync))
    application.add_handler(CommandHandler("deletesync", toggle_delete_sync))
    application.add_handler(CommandHandler("textonly", toggle_text_only))
    application.add_handler(CommandHandler("settings", check_settings))
    application.add_handler(CommandHandler("reset", reset_all))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("block", block_content))
    application.add_handler(CommandHandler("unblock", unblock_content))
    application.add_handler(CallbackQueryHandler(reset_callback, pattern="^reset_(confirm|cancel)$"))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("removeadmin", remove_admin))
    application.add_handler(CommandHandler("listadmins", list_admins))
    application.add_handler(CommandHandler("maxtasks", set_concurrent_tasks))

    # Start the bot
    await application.initialize()
    await application.start()
    
    # Polling se bot commands receive karo
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot started successfully with polling!")

    # Keep the client running
    if is_running:
        await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
