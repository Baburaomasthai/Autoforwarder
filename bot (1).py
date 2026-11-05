import os, time, asyncio, logging, threading
from datetime import datetime, timezone
from flask import Flask
from bson import ObjectId
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.error import TelegramError, RetryAfter, Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
OWNER_ID = int(os.getenv("OWNER_ID", "0"))       # your Telegram user ID (admin panel)
MAX_ROUTES = int(os.getenv("MAX_ROUTES", "5"))   # routes (source channels) per user
MAX_DESTS = int(os.getenv("MAX_DESTS", "5"))     # destinations per route

db = MongoClient(MONGO_URI)["fwdbot"]
routes, maps, settings = db["routes"], db["maps"], db["settings"]
routes.create_index("uid")
routes.create_index("source.id")
maps.create_index([("src_chat", 1), ("src", 1)])
maps.create_index("ts", expireAfterSeconds=7 * 86400)  # mappings expire after 7 days

sem = asyncio.Semaphore(15)  # global flood control
warned = set()


# ---------------- Styled font ----------------
# Telegram can't use custom fonts, so UI text is converted to Unicode
# "Sans-Serif Bold" characters (e.g. Hello -> 𝗛𝗲𝗹𝗹𝗼). Works in buttons too.
def _build_map():
    t = {}
    for i in range(26):
        t[ord("A") + i] = chr(0x1D5D4 + i)
        t[ord("a") + i] = chr(0x1D5EE + i)
    for i in range(10):
        t[ord("0") + i] = chr(0x1D7EC + i)
    return t


_MAP = _build_map()


def fx(s):
    return s.translate(_MAP)


async def safe(fn, *a, **k):
    for _ in range(3):
        try:
            async with sem:
                return await fn(*a, **k)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    raise TelegramError("flood retry failed")


def get_route(rid, uid):
    try:
        return routes.find_one({"_id": ObjectId(rid), "uid": uid})
    except Exception:
        return None


def onoff(v):
    return fx("ON") + " ✅" if v else fx("OFF") + " ❌"


# ---------------- Force-join gate ----------------
def fsub_list():
    d = settings.find_one({"_id": "fsub"})
    return d["channels"] if d else []


async def missing_channels(bot, uid):
    miss = []
    for ch in fsub_list():
        try:
            mem = await bot.get_chat_member(ch["id"], uid)
            if mem.status in ("left", "kicked") or (
                    mem.status == "restricted" and not mem.is_member):
                miss.append(ch)
        except TelegramError as e:  # e.g. bot removed from channel: don't lock users out
            logging.warning("Force-join check failed for %s: %s", ch["id"], e)
    return miss


def join_view(miss):
    text = (f"🔒 {fx('Access Locked')}\n\n"
            f"{fx('Join the channel(s) below to use this bot, then tap I Have Joined.')}")
    rows = [[B("📢 " + fx("Join") + f" {c['title']}", url=c["link"])] for c in miss]
    rows.append([B("✅ " + fx("I Have Joined"), callback_data="chk")])
    return text, M(rows)


async def gate(bot, uid):
    """Return a join view if the user must join something first, else None."""
    if uid == OWNER_ID:
        return None
    miss = await missing_channels(bot, uid)
    return join_view(miss) if miss else None


_mcache = {}


async def is_member_cached(bot, uid):
    if uid == OWNER_ID:
        return True
    t = _mcache.get(uid)
    if t and time.time() - t[0] < 300:
        return t[1]
    ok = not await missing_channels(bot, uid)
    _mcache[uid] = (time.time(), ok)
    return ok


# ---------------- Views ----------------
def home_view(uid):
    rs = list(routes.find({"uid": uid}))
    text = (f"⚙️ {fx('Auto Forward Bot')}\n\n"
            f"{fx('1. Make this bot an admin in your channels')}\n"
            f"{fx('2. Tap New Route and choose a source channel')}\n"
            f"{fx('3. Add destination channels to that route')}\n\n"
            f"{fx('Routes')}: {len(rs)}/{MAX_ROUTES}")
    rows = [[B(f"📥 {r['source']['title']} → {len(r['dests'])}", callback_data=f"r:{r['_id']}")]
            for r in rs]
    rows.append([B("➕ " + fx("New Route"), callback_data="new")])
    return text, M(rows)


def route_view(r):
    rid = str(r["_id"])
    dl = "\n".join(f"  • {d['title']}" for d in r["dests"]) or "  " + fx("None yet")
    text = (f"📥 {fx('Source')}: {r['source']['title']}\n\n"
            f"📤 {fx('Destinations')} ({len(r['dests'])}/{MAX_DESTS}):\n{dl}\n\n"
            f"✏️ {fx('Edit Sync')}: {onoff(r.get('edit_sync', True))}")
    rows = [[B(f"❌ {d['title']}", callback_data=f"rm:{rid}:{d['id']}")] for d in r["dests"]]
    if len(r["dests"]) < MAX_DESTS:
        rows.append([B("➕ " + fx("Add Destination"), callback_data=f"add:{rid}")])
    rows.append([B(f"✏️ {fx('Edit Sync')}: {onoff(r.get('edit_sync', True))}", callback_data=f"te:{rid}")])
    rows.append([B("🗑 " + fx("Delete Route"), callback_data=f"del:{rid}")])
    rows.append([B("⬅️ " + fx("Back"), callback_data="home")])
    return text, M(rows)


def admin_view():
    chs = fsub_list()
    text = (f"🛡 {fx('Admin Panel')}\n\n"
            f"{fx('Force-join channels')}: {len(chs)}\n"
            f"{fx('Users must join all of these to use the bot. Tap a channel to remove it.')}")
    rows = [[B(f"❌ {c['title']}", callback_data=f"fsrm:{c['id']}")] for c in chs]
    rows.append([B("➕ " + fx("Add Channel"), callback_data="fsadd")])
    rows.append([B("⬅️ " + fx("Back"), callback_data="home")])
    return text, M(rows)


async def show(q, view):
    text, kb = view
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except TelegramError:
        pass  # message not modified


# ---------------- Handlers ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("wait", None)
    uid = update.effective_user.id
    text, kb = await gate(ctx.bot, uid) or home_view(uid)
    await update.message.reply_text(text, reply_markup=kb)


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    ctx.user_data.pop("wait", None)
    text, kb = admin_view()
    await update.message.reply_text(text, reply_markup=kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    p = q.data.split(":")
    a = p[0]
    ctx.user_data.pop("wait", None)

    # "I Have Joined" button
    if a == "chk":
        _mcache.pop(uid, None)
        v = await gate(ctx.bot, uid)
        if v:
            await q.answer(fx("You haven't joined all channels yet."), show_alert=True)
            return await show(q, v)
        await q.answer()
        return await show(q, home_view(uid))

    # owner admin panel
    if a.startswith("fs"):
        if uid != OWNER_ID:
            return await q.answer()
        if a == "fsadd":
            ctx.user_data["wait"] = {"mode": "fsub"}
            view = (f"📢 {fx('Send the channel users must join')}\n\n"
                    f"{fx('Forward any post from it, or send its @username / ID.')}\n"
                    f"{fx('The bot must be an admin there (with Invite Users permission for private channels).')}",
                    M([[B("⬅️ " + fx("Cancel"), callback_data="fs")]]))
        elif a == "fsrm":
            settings.update_one({"_id": "fsub"}, {"$pull": {"channels": {"id": int(p[1])}}})
            _mcache.clear()
            view = admin_view()
        else:
            view = admin_view()
        await q.answer()
        return await show(q, view)

    # everything below requires the force-join check
    v = await gate(ctx.bot, uid)
    if v:
        await q.answer()
        return await show(q, v)

    if a == "home":
        view = home_view(uid)
    elif a == "new":
        if routes.count_documents({"uid": uid}) >= MAX_ROUTES:
            return await q.answer(fx(f"Limit reached: max {MAX_ROUTES} routes."), show_alert=True)
        ctx.user_data["wait"] = {"mode": "src"}
        view = (f"📥 {fx('Send the source channel')}\n\n"
                f"{fx('Forward any post from it, or send its @username / ID.')}\n"
                f"{fx('You and the bot must both be admins there.')}",
                M([[B("⬅️ " + fx("Cancel"), callback_data="home")]]))
    else:
        rid = p[1]
        r = get_route(rid, uid)
        if not r:
            await q.answer(fx("Route not found."), show_alert=True)
            return await show(q, home_view(uid))
        if a == "r":
            view = route_view(r)
        elif a == "add":
            ctx.user_data["wait"] = {"mode": "dest", "rid": rid}
            view = (f"📤 {fx('Send a destination channel')}\n\n"
                    f"{fx('Forward any post from it, or send its @username / ID.')}\n"
                    f"{fx('You can add several, one after another.')}",
                    M([[B("✅ " + fx("Done"), callback_data=f"r:{rid}")]]))
        elif a == "rm":
            routes.update_one({"_id": r["_id"]}, {"$pull": {"dests": {"id": int(p[2])}}})
            view = route_view(get_route(rid, uid))
        elif a == "te":
            routes.update_one({"_id": r["_id"]}, {"$set": {"edit_sync": not r.get("edit_sync", True)}})
            view = route_view(get_route(rid, uid))
        elif a == "del":
            view = (f"🗑 {fx('Delete this route?')}\n\n{r['source']['title']}",
                    M([[B("✅ " + fx("Yes, delete"), callback_data=f"delok:{rid}"),
                        B("❌ " + fx("No"), callback_data=f"r:{rid}")]]))
        elif a == "delok":
            routes.delete_one({"_id": r["_id"]})
            maps.delete_many({"rid": rid})
            view = home_view(uid)
        else:
            return await q.answer()
    await q.answer()
    await show(q, view)


async def resolve_channel(ctx, m, uid, need_user_admin=True):
    chat = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and fo.type == "channel":
        chat = fo.chat
    elif m.text:
        t = m.text.strip()
        try:
            chat = await ctx.bot.get_chat(int(t) if t.lstrip("-").isdigit() else t)
        except TelegramError:
            chat = None
    if not chat or chat.type != "channel":
        return None, "❌ " + fx("Channel not found. Forward a post from it or send a valid @username / ID.")
    try:
        me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
        if me.status != "administrator":
            raise TelegramError("bot not admin")
    except TelegramError:
        return None, "❌ " + fx("Please make the bot an admin in that channel first.")
    if need_user_admin:
        try:
            um = await ctx.bot.get_chat_member(chat.id, uid)
            if um.status not in ("creator", "administrator"):
                raise TelegramError("user not admin")
        except TelegramError:
            return None, "❌ " + fx("You are not an admin of that channel.")
    return chat, None


async def on_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    wait = ctx.user_data.get("wait")
    if not wait:
        return
    m, uid = update.message, update.effective_user.id

    # owner adds a force-join channel
    if wait["mode"] == "fsub":
        if uid != OWNER_ID:
            return
        chat, err = await resolve_channel(ctx, m, uid, need_user_admin=False)
        if err:
            return await m.reply_text(err)
        link = f"https://t.me/{chat.username}" if chat.username else None
        if not link:
            try:
                link = await ctx.bot.export_chat_invite_link(chat.id)
            except TelegramError:
                return await m.reply_text(
                    "❌ " + fx("Give the bot the Invite Users permission in that channel, then try again."))
        if any(c["id"] == chat.id for c in fsub_list()):
            return await m.reply_text("⚠️ " + fx("Already added."))
        settings.update_one({"_id": "fsub"},
                            {"$push": {"channels": {"id": chat.id, "title": chat.title, "link": link}}},
                            upsert=True)
        _mcache.clear()
        ctx.user_data.pop("wait", None)
        return await m.reply_text(f"✅ {fx('Added')}: {chat.title}",
                                  reply_markup=M([[B("🛡 " + fx("Admin Panel"), callback_data="fs")]]))

    v = await gate(ctx.bot, uid)
    if v:
        ctx.user_data.pop("wait", None)
        return await m.reply_text(v[0], reply_markup=v[1])

    chat, err = await resolve_channel(ctx, m, uid)
    if err:
        return await m.reply_text(err)
    item = {"id": chat.id, "title": chat.title}

    if wait["mode"] == "src":
        if routes.find_one({"uid": uid, "source.id": chat.id}):
            return await m.reply_text("⚠️ " + fx("A route for this source already exists. Add destinations inside it."))
        res = routes.insert_one({"uid": uid, "source": item, "dests": [], "edit_sync": True})
        rid = str(res.inserted_id)
        ctx.user_data["wait"] = {"mode": "dest", "rid": rid}
        return await m.reply_text(
            f"✅ {fx('Source set')}: {chat.title}\n\n"
            f"{fx('Now send the destination channels (forward a post or send @username / ID).')}",
            reply_markup=M([[B("✅ " + fx("Done"), callback_data=f"r:{rid}")]]))

    rid = wait["rid"]
    r = get_route(rid, uid)
    if not r:
        ctx.user_data.pop("wait", None)
        return await m.reply_text(fx("Route not found. Send /start."))
    if chat.id == r["source"]["id"]:
        return await m.reply_text("⚠️ " + fx("The source can't be a destination."))
    if any(d["id"] == chat.id for d in r["dests"]):
        return await m.reply_text("⚠️ " + fx("Already added."))
    if len(r["dests"]) >= MAX_DESTS:
        return await m.reply_text("⚠️ " + fx(f"Limit reached: max {MAX_DESTS} destinations."))
    routes.update_one({"_id": r["_id"]}, {"$push": {"dests": item}})
    await m.reply_text(f"✅ {fx('Added')}: {chat.title}\n{fx('Send more or tap Done.')}",
                       reply_markup=M([[B("✅ " + fx("Done"), callback_data=f"r:{rid}")]]))


# ---------------- Forwarding ----------------
albums = {}


async def send_route(bot, r, src_id, ids):
    rid = str(r["_id"])
    for d in r["dests"]:
        try:
            if len(ids) == 1:
                res = [await safe(bot.copy_message, d["id"], src_id, ids[0])]
            else:
                res = await safe(bot.copy_messages, d["id"], src_id, ids)
            now = datetime.now(timezone.utc)
            maps.insert_many([
                {"rid": rid, "src_chat": src_id, "src": s, "dest": d["id"],
                 "dmsg": x.message_id, "ts": now}
                for s, x in zip(ids, res)])
        except Forbidden:
            if (rid, d["id"]) not in warned:
                warned.add((rid, d["id"]))
                try:
                    await bot.send_message(
                        r["uid"], "⚠️ " + fx(f"The bot can't post in {d['title']}. Check its admin permissions."))
                except TelegramError:
                    pass
        except TelegramError as e:
            logging.warning("Forward error %s: %s", d["id"], e)


async def dispatch(bot, src_id, ids):
    for r in routes.find({"source.id": src_id}):
        if not r["dests"]:
            continue
        if not await is_member_cached(bot, r["uid"]):  # owner left the force-join channel(s)
            continue
        asyncio.create_task(send_route(bot, r, src_id, ids))


async def flush_album(bot, src_id, gid):
    await asyncio.sleep(2)  # let all album parts arrive
    ids = sorted(albums.pop((src_id, gid)))
    await dispatch(bot, src_id, ids)


async def on_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.channel_post
    if not routes.find_one({"source.id": m.chat_id}):
        return
    gid = m.media_group_id
    if gid:
        key = (m.chat_id, gid)
        if key in albums:
            albums[key].append(m.message_id)
        else:
            albums[key] = [m.message_id]
            ctx.application.create_task(flush_album(ctx.bot, m.chat_id, gid))
        return
    await dispatch(ctx.bot, m.chat_id, [m.message_id])


async def on_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.edited_channel_post
    for r in routes.find({"source.id": m.chat_id}):
        if not r.get("edit_sync", True):
            continue
        if not await is_member_cached(ctx.bot, r["uid"]):
            continue
        for mp in maps.find({"rid": str(r["_id"]), "src": m.message_id}):
            try:
                if m.text:
                    await safe(ctx.bot.edit_message_text, m.text_html, mp["dest"], mp["dmsg"],
                               parse_mode="HTML")
                else:
                    await safe(ctx.bot.edit_message_caption, mp["dest"], mp["dmsg"],
                               caption=m.caption_html or "", parse_mode="HTML")
            except TelegramError as e:
                logging.warning("Edit error %s: %s", mp["dest"], e)


# ---------------- Main ----------------
web = Flask(__name__)


@web.route("/")
def home():
    return "Bot is running"


def run_web():
    web.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


def main():
    threading.Thread(target=run_web, daemon=True).start()  # keeps Render Web Service happy
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("admin", admin, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, on_edit))
    app.run_polling(allowed_updates=["message", "callback_query",
                                     "channel_post", "edited_channel_post"])


if __name__ == "__main__":
    main()
