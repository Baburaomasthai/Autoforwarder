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
from telethon.tl.types import MessageMediaWebPage, MessageEntityTextUrl, MessageEntityUrl, MessageEntityMention
from telethon import types as tl_types
from telethon.helpers import add_surrogate, del_surrogate
import copy
import io
import asyncio
from difflib import SequenceMatcher
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

DEFAULT_LINK_PLACEHOLDER = "[Link Removed]"

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

    async def get_edits_and_links(self, source_id=None, target_id=None):
        """Edits + links return karta hai. Links mein pehle us pair ke specific links, phir global links aate hain."""
        try:
            edits = []
            pair_links = []
            global_links = []

            async for doc in db.edits.find({}):
                edits.append((doc['old_text'], doc['new_text']))

            async for doc in db.links.find({}):
                scope_source = doc.get('source_channel_id')
                scope_target = doc.get('target_channel_id')
                item = (doc['old_link'], doc['new_link'])
                if scope_source is None:
                    global_links.append(item)
                elif source_id is not None and scope_source == source_id and scope_target == target_id:
                    pair_links.append(item)

            return edits, pair_links + global_links
        except Exception as e:
            logger.error(f"Error getting edits and links: {e}")
            return [], []

    async def get_link_filter(self, source_id, target_id):
        """Is pair ke liye link filter setting: {"enabled": bool, "placeholder": str}"""
        enabled = False
        placeholder = DEFAULT_LINK_PLACEHOLDER
        try:
            global_doc = await db.link_filters.find_one({"source_channel_id": None, "target_channel_id": None})
            pair_doc = await db.link_filters.find_one({"source_channel_id": source_id, "target_channel_id": target_id})

            if global_doc:
                enabled = bool(global_doc.get("enabled", False))
                if global_doc.get("placeholder") is not None:
                    placeholder = global_doc["placeholder"]
            if pair_doc:
                if "enabled" in pair_doc:
                    enabled = bool(pair_doc["enabled"])
                if pair_doc.get("placeholder") is not None:
                    placeholder = pair_doc["placeholder"]
        except Exception as e:
            logger.error(f"Error getting link filter: {e}")
        return {"enabled": enabled, "placeholder": placeholder}

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

# Dhyan: IGNORECASE poore pattern par mat lagao, warna negated class mein 's'/'k' jaise letters kat jaate hain
URL_REGEX = re.compile(r"(?i:https?://|www\.|t\.me/|telegram\.me/|tg://)[^\s<>\"'\u0080-\uffff]+")
URL_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""
# @username mentions (email jaise a@b.com ko chhodkar)
MENTION_REGEX = re.compile(r"(?<![\w@/.])@[A-Za-z][A-Za-z0-9_]{3,31}")
TME_USERNAME_REGEX = re.compile(r"^(?:t\.me|telegram\.me)/([a-z][a-z0-9_]{3,31})(?:[/?#].*)?$")

def normalize_link(url):
    """Link ko compare karne ke liye saaf form: lowercase, bina http(s)/www aur trailing slash ke"""
    u = (url or "").strip().lower()
    u = re.sub(r"^(?:https?://|tg://)", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")

def is_allowed_link(url, allowed_norms):
    """Link/@username tabhi allowed hai jab wo /addlink mein set kisi link se match kare"""
    n = normalize_link(url)

    # @username aur t.me/username ko ek hi channel maana jata hai
    username = None
    if n.startswith("@"):
        username = n[1:]
    else:
        m = TME_USERNAME_REGEX.match(n)
        if m:
            username = m.group(1)
    if username and any(a in allowed_norms for a in (f"@{username}", f"t.me/{username}", f"telegram.me/{username}")):
        return True

    for a in allowed_norms:
        if not a:
            continue
        if n == a:
            return True
        if n.startswith(a) and n[len(a)] in "/?#&=:;":
            return True
    return False

def find_link_spans(current, ents):
    """Text mein jitne bhi visible links hain unke (start, end) spans (UTF-16 surrogate text par)"""
    spans = []
    for m in URL_REGEX.finditer(current):
        start, end = m.start(), m.end()
        while end > start and current[end - 1] in URL_TRAILING_PUNCTUATION:
            end -= 1
        if end > start:
            spans.append((start, end))
    for m in MENTION_REGEX.finditer(current):
        spans.append((m.start(), m.end()))
    for ent in ents:
        if isinstance(ent, (MessageEntityUrl, MessageEntityMention)):
            spans.append((ent.offset, ent.offset + ent.length))

    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged

def apply_link_filter(current, ents, allowed_norms, placeholder):
    """Jo link allowed nahi hai usko placeholder text se replace karo (visible aur hidden dono links)"""
    placeholder_s = add_surrogate(placeholder or "")
    matches = []
    kept = []

    text_url_ranges = [(e.offset, e.offset + e.length) for e in ents if isinstance(e, MessageEntityTextUrl)]

    # Hidden links (jaise "Download" ke peeche chhupa URL)
    for ent in ents:
        if isinstance(ent, MessageEntityTextUrl):
            if not is_allowed_link(ent.url, allowed_norms):
                matches.append((ent.offset, ent.offset + ent.length, placeholder_s))
                continue  # entity hata do, label placeholder ban jayega
        kept.append(ent)

    # Visible links
    for start, end in find_link_spans(current, ents):
        if any(start < r_end and end > r_start for r_start, r_end in text_url_ranges):
            continue  # ye hidden link ka label hai, upar handle ho chuka
        if not is_allowed_link(current[start:end], allowed_norms):
            matches.append((start, end, placeholder_s))

    if not matches:
        return current, kept

    # Hata hue links ki auto-link entities bhi hata do
    kept = [
        e for e in kept
        if not (isinstance(e, (MessageEntityUrl, MessageEntityMention))
                and any(e.offset < m_end and e.offset + e.length > m_start for m_start, m_end, _ in matches))
    ]
    return _apply_matches(current, kept, matches)

def apply_rules_with_entities(text, entities, edits, links, link_filter=None):
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

    # Unknown links hatao (sirf /addlink wale links hi pass hote hain)
    if link_filter and link_filter.get("enabled"):
        allowed_norms = []
        for old_link, new_link in links:
            allowed_norms.append(normalize_link(old_link))
            allowed_norms.append(normalize_link(new_link))
        current, ents = apply_link_filter(current, ents, allowed_norms, link_filter.get("placeholder", ""))

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

# ---------------------------------------------------------------------------
# Image tools: fixed-area blur + OCR based skip
# ---------------------------------------------------------------------------
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")  # https://ocr.space/ocrapi se free key lo

BLUR_PRESETS = {
    "bottomright": (65, 88, 35, 12),
    "bottomleft": (0, 88, 35, 12),
    "topright": (65, 0, 35, 12),
    "topleft": (0, 0, 35, 12),
    "top": (0, 0, 100, 12),
    "bottom": (0, 88, 100, 12),
    "center": (25, 40, 50, 20),
}

def is_static_image(message):
    """Photo ya image-file hai to True (sticker/gif/video nahi)"""
    if getattr(message, "photo", None):
        return True
    doc = getattr(message, "document", None)
    if doc and not getattr(message, "sticker", None):
        return (getattr(doc, "mime_type", "") or "") in ("image/jpeg", "image/png", "image/webp")
    return False

def compact_text(text):
    """Spaces, symbols, case hata ke compare ke liye"""
    return re.sub(r"[\W_]+", "", (text or "").lower())

def text_matches_keyword(ocr_text, keyword):
    """OCR text mein keyword hai? (chhoti OCR galtiyon ko bhi maaf karta hai)"""
    text, key = compact_text(ocr_text), compact_text(keyword)
    if not key or not text:
        return False
    if key in text:
        return True
    if len(key) < 6:
        return False
    n = len(key)
    for size in (n - 1, n, n + 1):
        for i in range(0, max(0, len(text) - size) + 1):
            if SequenceMatcher(None, text[i:i + size], key).ratio() >= 0.85:
                return True
    return False

def blur_regions_sync(image_bytes, areas):
    """areas = [(x%, y%, w%, h%)]. Un jagahon ko heavy blur karke JPEG bytes return karta hai"""
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    width, height = img.size

    for x, y, w, h in areas:
        left = max(0, int(width * x / 100))
        top = max(0, int(height * y / 100))
        right = min(width, int(width * (x + w) / 100))
        bottom = min(height, int(height * (y + h) / 100))
        if right - left < 2 or bottom - top < 2:
            continue
        region = img.crop((left, top, right, bottom))
        rw, rh = region.size
        small = region.resize((max(2, rw // 8), max(2, rh // 8)), Image.BILINEAR)
        region = small.resize((rw, rh), Image.BICUBIC)
        region = region.filter(ImageFilter.GaussianBlur(radius=max(2, min(rw, rh) // 10)))
        img.paste(region, (left, top))

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()

def shrink_for_ocr_sync(image_bytes):
    """OCR.space free limit (1MB) ke liye image chhoti karo"""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1800, 1800))
    quality = 85
    while True:
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality)
        if out.tell() <= 900_000 or quality <= 40:
            return out.getvalue()
        quality -= 15

async def ocr_image_text(image_bytes):
    """Image ke andar ka text (OCR.space free API). Fail ho to None"""
    if not OCR_SPACE_API_KEY:
        return None
    try:
        import httpx
        loop = asyncio.get_running_loop()
        small = await loop.run_in_executor(None, shrink_for_ocr_sync, image_bytes)
        async with httpx.AsyncClient(timeout=40) as http:
            response = await http.post(
                "https://api.ocr.space/parse/image",
                data={"apikey": OCR_SPACE_API_KEY, "language": "eng", "OCREngine": "2", "scale": "true"},
                files={"file": ("image.jpg", small, "image/jpeg")},
            )
        result = response.json()
        if result.get("IsErroredOnProcessing"):
            logger.warning(f"OCR error: {result.get('ErrorMessage')}")
            return None
        parsed = result.get("ParsedResults") or []
        return " ".join(item.get("ParsedText", "") for item in parsed)
    except Exception as e:
        logger.warning(f"OCR request failed: {e}")
        return None

async def prepare_media_for_forward(message, media, source_id):
    """Returns (media_to_send, skipped). Photo par fixed-area blur lagata hai, phir OCR se naam mile to skip.
    Koi bhi dikkat aaye to original media hi forward hota hai (post miss nahi hoti)."""
    if not media or not is_static_image(message):
        return media, False

    try:
        areas = []
        async for doc in db.blur_areas.find({"source_channel_id": source_id}):
            areas.append((doc["x"], doc["y"], doc["w"], doc["h"]))

        skip_words = []
        if OCR_SPACE_API_KEY:
            async for doc in db.skip_words.find({}):
                skip_words.append(doc.get("word", ""))

        if not areas and not skip_words:
            return media, False

        data = await client.download_media(message, file=bytes)
        if not data:
            return media, False

        loop = asyncio.get_running_loop()
        result = media
        if areas:
            data = await loop.run_in_executor(None, blur_regions_sync, data, areas)
            result = data  # bytes

        # Blur ke baad OCR: fixed jagah wala naam hat chuka hota hai, kahin aur naam ho to pakda jayega
        if skip_words:
            text = await ocr_image_text(data)
            if text and any(text_matches_keyword(text, w) for w in skip_words):
                logger.info(f"Image skipped (skip word found in OCR) from {source_id}")
                return None, True

        return result, False

    except Exception as e:
        logger.error(f"Image preparation failed, sending original: {e}")
        return media, False

def to_sendable_file(item):
    """bytes ko Telegram photo ki tarah bhejne layak file object banao"""
    if isinstance(item, (bytes, bytearray)):
        bio = io.BytesIO(bytes(item))
        bio.name = "photo.jpg"
        return bio
    return item

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

def parse_scope_args(args):
    """[source_id target_id] prefix ho to nikaalo. Return: (source_id, target_id, baaki_args)"""
    if len(args) >= 2 and re.fullmatch(r"-?\d+", args[0]) and re.fullmatch(r"-?\d+", args[1]):
        return int(args[0]), int(args[1]), list(args[2:])
    return None, None, list(args)

def pair_exists(source_id, target_id):
    return target_id in bot.channel_pairs.get(source_id, [])

@admin_required
async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        source_id, target_id, rest = parse_scope_args(context.args)

        if len(rest) != 2:
            await update.message.reply_text(
                "↳ Format:\n"
                "/addlink source_id target_id old_link new_link  (sirf us pair ke liye)\n"
                "Link ki jagah @username bhi de sakte ho: /addlink source_id target_id @old @new\n"
                "/addlink old_link new_link  (sab pairs ke liye)\n\n"
                "Pair ke liye IDs /listpairs se lo."
            )
            return

        old_link, new_link = rest[0].strip(), rest[1].strip()
        if not old_link or not new_link:
            await update.message.reply_text("❌ Both links required")
            return

        if source_id is not None and not pair_exists(source_id, target_id):
            await update.message.reply_text("❌ Ye channel pair set nahi hai. /listpairs se IDs check karo.")
            return

        await db.links.update_one(
            {"old_link": old_link, "source_channel_id": source_id, "target_channel_id": target_id},
            {"$set": {"new_link": new_link}},
            upsert=True
        )

        scope = f"{source_id} → {target_id}" if source_id is not None else "All pairs"
        await update.message.reply_text(f"✅ Link replacement added ({scope}):\n{old_link} → {new_link}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        source_id, target_id, rest = parse_scope_args(context.args)
        if len(rest) != 1:
            await update.message.reply_text(
                "➢ Format:\n/removelink source_id target_id old_link\n/removelink old_link  (global link ke liye)"
            )
            return

        old_link = rest[0].strip()
        result = await db.links.delete_many(
            {"old_link": old_link, "source_channel_id": source_id, "target_channel_id": target_id}
        )
        if result.deleted_count:
            await update.message.reply_text(f"✅ Link replacement removed: {old_link}")
        else:
            await update.message.reply_text("🤔 Ye link is scope mein nahi mila. /showlinks se check karo.")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /removeword old_text")
            return

        old_text = ' '.join(context.args)
        result = await db.edits.delete_many({"old_text": old_text, "new_text": {"$ne": ""}})
        if result.deleted_count:
            await update.message.reply_text(f"✅ Replacement removed: {old_text}")
        else:
            await update.message.reply_text("🤔 Ye replacement nahi mila. /showreps se check karo. (Blocked text ke liye /unblock use karo)")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def link_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        source_id, target_id, rest = parse_scope_args(context.args)
        if len(rest) != 1 or rest[0].lower() not in ("on", "off"):
            await update.message.reply_text(
                "➢ Format:\n"
                "/linkfilter source_id target_id on|off  (ek pair ke liye)\n"
                "/linkfilter on|off  (sab pairs ke liye default)\n\n"
                "ON hone par sirf /addlink mein set links aur @usernames hi forward hote hain, baaki links aur @mentions placeholder text se replace ho jaate hain.\n"
                "Status dekhne ke liye /showlinks."
            )
            return

        if source_id is not None and not pair_exists(source_id, target_id):
            await update.message.reply_text("❌ Ye channel pair set nahi hai. /listpairs se IDs check karo.")
            return

        enabled = rest[0].lower() == "on"
        await db.link_filters.update_one(
            {"source_channel_id": source_id, "target_channel_id": target_id},
            {"$set": {"enabled": enabled}},
            upsert=True
        )
        scope = f"{source_id} → {target_id}" if source_id is not None else "All pairs (default)"
        await update.message.reply_text(f"✅ Link filter {'ON 🔒' if enabled else 'OFF 🔓'} ({scope})")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def link_placeholder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        source_id, target_id, rest = parse_scope_args(context.args)
        if not rest:
            await update.message.reply_text(
                "➢ Format:\n"
                "/linkplaceholder source_id target_id text  (ek pair ke liye)\n"
                "/linkplaceholder text  (sab pairs ke liye default)\n"
                "/linkplaceholder none  (link bas hata do, kuch mat likho)\n"
                "/linkplaceholder reset  (default text par wapas)\n\n"
                f"Default text: {DEFAULT_LINK_PLACEHOLDER}"
            )
            return

        if source_id is not None and not pair_exists(source_id, target_id):
            await update.message.reply_text("❌ Ye channel pair set nahi hai. /listpairs se IDs check karo.")
            return

        text = " ".join(rest).strip()
        query = {"source_channel_id": source_id, "target_channel_id": target_id}
        scope = f"{source_id} → {target_id}" if source_id is not None else "All pairs (default)"

        if text.lower() == "reset":
            await db.link_filters.update_one(query, {"$unset": {"placeholder": ""}}, upsert=True)
            await update.message.reply_text(f"✅ Placeholder reset ({scope})")
            return

        if text.lower() == "none":
            text = ""

        await db.link_filters.update_one(query, {"$set": {"placeholder": text}}, upsert=True)
        shown = text if text else "(khali, link bas hata diya jayega)"
        await update.message.reply_text(f"✅ Placeholder set ({scope}):\n{shown}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def show_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        global_links = []
        pair_links = {}
        async for doc in db.links.find({}):
            item = (doc.get("old_link", ""), doc.get("new_link", ""))
            if doc.get("source_channel_id") is None:
                global_links.append(item)
            else:
                pair_links.setdefault((doc["source_channel_id"], doc.get("target_channel_id")), []).append(item)

        global_filter = {"enabled": False, "placeholder": DEFAULT_LINK_PLACEHOLDER}
        pair_filters = {}
        async for doc in db.link_filters.find({}):
            if doc.get("source_channel_id") is None:
                global_filter["enabled"] = bool(doc.get("enabled", False))
                if doc.get("placeholder") is not None:
                    global_filter["placeholder"] = doc["placeholder"]
            else:
                pair_filters[(doc["source_channel_id"], doc.get("target_channel_id"))] = doc

        def shown_placeholder(text):
            return text if text else "(khali)"

        lines = ["🔗 Link Settings", ""]
        lines.append(f"🌐 All pairs: filter {'ON 🔒' if global_filter['enabled'] else 'OFF 🔓'} | placeholder: {shown_placeholder(global_filter['placeholder'])}")
        if global_links:
            lines.extend(f"   {i}. {o} → {n}" for i, (o, n) in enumerate(global_links, 1))
        else:
            lines.append("   (koi global link replacement nahi)")

        for key in sorted(set(pair_links) | set(pair_filters)):
            source_id, target_id = key
            fdoc = pair_filters.get(key, {})
            if "enabled" in fdoc:
                state = "ON 🔒" if fdoc["enabled"] else "OFF 🔓"
            else:
                state = ("ON 🔒" if global_filter["enabled"] else "OFF 🔓") + " (default)"
            placeholder = fdoc.get("placeholder")
            if placeholder is None:
                placeholder = global_filter["placeholder"]
            lines.append("")
            lines.append(f"📍 {source_id} → {target_id}: filter {state} | placeholder: {shown_placeholder(placeholder)}")
            items = pair_links.get(key, [])
            if items:
                lines.extend(f"   {i}. {o} → {n}" for i, (o, n) in enumerate(items, 1))
            else:
                lines.append("   (is pair ka koi link set nahi)")

        await send_long_message(update, "\n".join(lines))

    except Exception as e:
        logger.error(f"Error in show_links: {e}")
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
            scope = "" if doc.get("source_channel_id") is None else f"  [{doc['source_channel_id']}→{doc.get('target_channel_id')}]"
            links.append((doc.get("old_link", ""), f"{doc.get('new_link', '')}{scope}"))

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
        for name in ("channel_pairs", "edits", "links", "settings", "message_mapping", "blur_areas", "skip_words", "link_filters"):
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
async def add_blur_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usage = (
        "➢ Format: /blurarea source_channel_id preset\n"
        "➢ Ya: /blurarea source_channel_id x y width height  (image ka % mein, top-left se)\n\n"
        f"Presets: {', '.join(BLUR_PRESETS)}\n"
        "Example: /blurarea -1001234567890 bottomright\n"
        "Example: /blurarea -1001234567890 60 85 40 15"
    )
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text(usage)
            return

        source_id = int(args[0])
        if len(args) == 2:
            preset = args[1].lower()
            if preset not in BLUR_PRESETS:
                await update.message.reply_text("❌ Unknown preset.\n\n" + usage)
                return
            x, y, w, h = BLUR_PRESETS[preset]
        elif len(args) == 5:
            x, y, w, h = (float(v) for v in args[1:5])
        else:
            await update.message.reply_text(usage)
            return

        if not (0 <= x < 100 and 0 <= y < 100 and w > 0 and h > 0 and x + w <= 100.01 and y + h <= 100.01):
            await update.message.reply_text("❌ Values galat hain. x,y 0-100 ke beech, aur x+width, y+height 100 se zyada nahi hone chahiye.")
            return

        await db.blur_areas.insert_one({"source_channel_id": source_id, "x": x, "y": y, "w": w, "h": h})
        await update.message.reply_text(
            f"✅ Blur area added for {source_id}\nx={x}%, y={y}%, width={w}%, height={h}%\n\n"
            "Ab is source ki saari photos mein ye jagah blur hogi. Sahi na lage to /removeblur se hata ke dobara try karo."
        )
    except ValueError:
        await update.message.reply_text("❌ Channel ID aur numbers sahi daalo.\n\n" + usage)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def show_blur_areas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        lines = []
        async for doc in db.blur_areas.find({}):
            lines.append(f"{doc['source_channel_id']}: x={doc['x']}%, y={doc['y']}%, w={doc['w']}%, h={doc['h']}%")
        if not lines:
            await update.message.reply_text("❌ Koi blur area set nahi hai")
            return
        text = "🌫 Blur Areas:\n" + "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
        await send_long_message(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_blur_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("➢ Format: /removeblur source_channel_id\n(Us source ke saare blur areas hat jayenge)\n➢ Sab hatane ke liye: /removeblur all")
            return
        if context.args[0].lower() == "all":
            result = await db.blur_areas.delete_many({})
        else:
            result = await db.blur_areas.delete_many({"source_channel_id": int(context.args[0])})
        await update.message.reply_text(f"✅ {result.deleted_count} blur area(s) removed")
    except ValueError:
        await update.message.reply_text("❌ Sahi channel ID daalo")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def add_skip_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        word = get_command_argument_text(update)
        if not word:
            await update.message.reply_text("➢ Format: /addskip text\nExample: /addskip Jaggi Gift Area\n\nJis image ke andar ye text mile wo forward nahi hogi.")
            return
        if not OCR_SPACE_API_KEY:
            await update.message.reply_text("⚠️ OCR_SPACE_API_KEY environment variable set nahi hai, isliye image skip kaam nahi karega. Free key: https://ocr.space/ocrapi")
        async for doc in db.skip_words.find({}):
            if compact_text(doc.get("word")) == compact_text(word):
                await update.message.reply_text("🤔 Ye skip word pehle se hai")
                return
        await db.skip_words.insert_one({"word": word})
        await update.message.reply_text(f"✅ Skip word added: {word}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def show_skip_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        words = [doc.get("word", "") async for doc in db.skip_words.find({})]
        if not words:
            await update.message.reply_text("❌ Koi skip word set nahi hai")
            return
        status = "ON ✅" if OCR_SPACE_API_KEY else "OFF ❌ (OCR_SPACE_API_KEY set nahi hai)"
        text = f"🚫 Image Skip Words (OCR {status}):\n" + "\n".join(f"{i}. {w}" for i, w in enumerate(words, 1))
        await send_long_message(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def remove_skip_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        word = get_command_argument_text(update)
        if not word:
            await update.message.reply_text("➢ Format: /removeskip text\n➢ Sab hatane ke liye: /removeskip all")
            return
        if word.lower() == "all":
            result = await db.skip_words.delete_many({})
            await update.message.reply_text(f"✅ {result.deleted_count} skip word(s) removed")
            return
        ids = [doc["_id"] async for doc in db.skip_words.find({}) if compact_text(doc.get("word")) == compact_text(word)]
        if ids:
            await db.skip_words.delete_many({"_id": {"$in": ids}})
            await update.message.reply_text(f"✅ Removed: {word}")
        else:
            await update.message.reply_text("🤔 Ye skip word list mein nahi mila. /showskip se check karo.")
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
        "• /addlink - Add link replacement (pair-wise ya global)\n"
        "• /removeword - Remove text replacement\n"
        "• /removelink - Remove link replacement\n"
        "• /showreps - Show all replacements\n"
        "• /linkfilter - Sirf /addlink wale links/@usernames allow karo (baaki hat jayenge)\n"
        "• /linkplaceholder - Hate gaye link ki jagah ka text set karo\n"
        "• /showlinks - Link rules, filter aur placeholder dekho\n"
        "• /forward - Toggle forwarding on/off\n"
        "• /editsync - Toggle edit sync\n"
        "• /deletesync - Toggle delete sync\n"
        "• /textonly - Toggle text only mode\n"
        "• /settings - Show current settings\n"
        "• /block - Block specific content\n"
        "• /unblock - Unblock blocked content (or /unblock all)\n"
        "• /blurarea, /showblur, /removeblur - Photos mein fixed jagah blur karo\n"
        "• /addskip, /showskip, /removeskip - Image ke andar text mile to skip karo\n"
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
            edits, links = await bot.get_edits_and_links(source_id, target_id)
            link_filter = await bot.get_link_filter(source_id, target_id)
            processed_text, entities = apply_rules_with_entities(
                message_text, event.message.entities, edits, links, link_filter
            )

            # Blocking ke baad kuch bacha hi nahi to empty message mat bhejo
            if not processed_text.strip() and not media:
                return None

            media_to_send, image_skipped = await prepare_media_for_forward(event.message, media, source_id)
            if image_skipped and not processed_text.strip():
                return None

            async def send(entities_to_use):
                if media_to_send and not bot.text_only:
                    return await client.send_file(
                        target_id, to_sendable_file(media_to_send), caption=processed_text,
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
    edits, links = await bot.get_edits_and_links(source_id, target_id)
    link_filter = await bot.get_link_filter(source_id, target_id)

    async with bot.semaphore:
        combined_text = next((m.raw_text for m in messages if m.raw_text), "")
        if not bot.should_forward_message(combined_text, source_id, target_id):
            logger.info(f"Skipping album due to filter: {source_id} -> {target_id}")
            return []

        processed = []
        for m in messages:
            text, entities = apply_rules_with_entities(m.raw_text or "", m.entities, edits, links, link_filter)
            processed.append((m, get_real_media(m), text, entities))

        # Ek ek karke (OCR free API ki rate limit se bachne ke liye)
        prepared = []
        for item in processed:
            prepared.append(await prepare_media_for_forward(item[0], item[1], source_id))

        media_items = [
            (item[0], new_media, item[2], item[3])
            for item, (new_media, skipped) in zip(processed, prepared)
            if item[1] and not skipped
        ]

        # Skip hui image ka caption kho na jaye: pehli bachi hui image par laga do
        if media_items and not any(it[2].strip() for it in media_items):
            lost = next(((it[2], it[3]) for it, (_, skipped) in zip(processed, prepared)
                         if skipped and it[2].strip()), None)
            if lost:
                first = media_items[0]
                media_items[0] = (first[0], first[1], lost[0], lost[1])

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
                target_id, [to_sendable_file(item[1]) for item in media_items],
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

async def edit_one_target(source_id, target_channel_id, target_message_id, message):
    """Ek target message ko us pair ke rules (replacements + link filter) ke saath edit karo"""
    edits, links = await bot.get_edits_and_links(source_id, target_channel_id)
    link_filter = await bot.get_link_filter(source_id, target_channel_id)
    text, entities = apply_rules_with_entities(
        message.raw_text or "", message.entities, edits, links, link_filter
    )
    return await edit_target_message(target_channel_id, target_message_id, text, entities)

@client.on(events.MessageEdited)
async def handle_edit(event):
    if not bot.forwarding_enabled or not bot.edit_sync:
        return
        
    try:
        source_id = event.chat_id
        if source_id not in bot.message_mapping or event.message.id not in bot.message_mapping[source_id]:
            return
            
        # Get all target messages that need to be updated
        target_messages = bot.message_mapping[source_id][event.message.id]
        
        # Har target ke apne link rules hote hain, isliye har target ke liye alag process karo
        tasks = []
        for target_channel_id, target_message_id in target_messages.items():
            task = asyncio.create_task(
                edit_one_target(source_id, target_channel_id, target_message_id, event.message)
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
    application.add_handler(CommandHandler("linkfilter", link_filter_command))
    application.add_handler(CommandHandler("linkplaceholder", link_placeholder_command))
    application.add_handler(CommandHandler("showlinks", show_links))
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
    application.add_handler(CommandHandler("blurarea", add_blur_area))
    application.add_handler(CommandHandler("showblur", show_blur_areas))
    application.add_handler(CommandHandler("removeblur", remove_blur_area))
    application.add_handler(CommandHandler("addskip", add_skip_word))
    application.add_handler(CommandHandler("showskip", show_skip_words))
    application.add_handler(CommandHandler("removeskip", remove_skip_word))
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
