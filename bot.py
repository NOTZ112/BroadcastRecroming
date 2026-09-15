import os
import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ============================================================
# CONFIG / LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("promotion-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "promotion_bot")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

CHAT_SEND_DELAY = 1.2
USER_SEND_DELAY = 1.5
PROGRESS_EVERY = 10
MAX_RETRIES = 3

WELCOME_TEXT = (
    "👋 Welcome!\n\n"
    "🎬 പുതിയ വീഡിയോകൾ\n"
    "👥 നല്ല ഗ്രൂപ്പുകൾ\n"
    "📢 പുതിയ ചാനലുകൾ\n\n"
    "എല്ലാം ഇവിടെ ലഭിക്കും ❤️"
)

INTERVALS = {
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}
SET_NUMBERS = [1, 2, 3, 4, 5]

# ============================================================
# NEW AUTPOST SETTINGS
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

AUTPOST_CHECK_INTERVAL = 5
AUTOPOST_MAX_INTERVAL = 1440

mongo = None
db = None
users_col = None
chats_col = None
schedules_col = None
sets_col = None
autoposts_col = None


def now():
    return datetime.now(timezone.utc)


def ist_now():
    return datetime.now(IST)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, context)
    return wrapper


def valid_url(url: str) -> bool:
    return bool(url) and (
        url.startswith("https://")
        or url.startswith("http://")
        or url.startswith("tg://")
    )


def build_payload(setup):
    buttons = setup.get("buttons")
    if buttons is None:
        buttons = []
        old_text = setup.get("button_text")
        old_url = setup.get("button_url")
        if old_text and old_url:
            buttons.append({
                "text": old_text,
                "url": old_url,
                "style": "primary",
            })
    return {
        "photo_file_id": setup.get("photo_file_id"),
        "text": setup.get("text") or "",
        "buttons": buttons[:3],
    }


def build_keyboard(payload):
    buttons = payload.get("buttons") or []
    if not buttons:
        return None

    keyboard = []
    for button in buttons[:3]:
        text = (button.get("text") or "").strip()
        url = (button.get("url") or "").strip()
        style = (button.get("style") or "primary").lower().strip()

        if not text or not url:
            continue
        if style not in ("primary", "success", "danger"):
            style = "primary"

        keyboard.append([
            InlineKeyboardButton(
                text=text[:64],
                url=url,
                style=style,
            )
        ])

    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def send_payload(bot, chat_id, payload):
    keyboard = build_keyboard(payload)
    photo_id = payload.get("photo_file_id")
    text = payload.get("text") or ""

    if photo_id:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=text[:1024] if text else None,
            reply_markup=keyboard,
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text[:4096] if text else " ",
        reply_markup=keyboard,
    )


# ============================================================
# CHAT REGISTRATION
# ============================================================

async def register_chat(chat):
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return

    try:
        await chats_col.update_one(
            {"chat_id": chat.id},
            {
                "$set": {
                    "chat_id": chat.id,
                    "title": chat.title or str(chat.id),
                    "type": chat.type,
                    "enabled": True,
                    "updated_at": now(),
                },
                "$setOnInsert": {"created_at": now()},
            },
            upsert=True,
        )
    except Exception as e:
        log.error("Register chat error: %s", e)


async def my_chat_member_update(update, context):
    chat = update.effective_chat
    member_update = update.my_chat_member
    if not chat or not member_update:
        return

    new_status = member_update.new_chat_member.status

    if new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        await register_chat(chat)

        if chat.type in ("group", "supergroup"):
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "✅ Promotion system activated.\n\n"
                        "This chat has been registered automatically."
                    ),
                )
            except Exception as e:
                log.info("Activation message skipped: %s", e)

    elif new_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.BANNED,
        "kicked",
    ):
        try:
            await chats_col.update_one(
                {"chat_id": chat.id},
                {"$set": {"enabled": False, "updated_at": now()}},
            )
        except Exception as e:
            log.error("Disable chat error: %s", e)


async def chat_member_update(update, context):
    if update.effective_chat:
        await register_chat(update.effective_chat)


# ============================================================
# START / USERS / CHATS
# ============================================================

async def start(update, context):
    user = update.effective_user
    if not user:
        return

    existing = await users_col.find_one({"user_id": user.id})

    if existing:
        await users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "name": user.full_name,
                "username": user.username,
                "last_seen_at": now(),
                "blocked": False,
            }},
        )
        return

    await users_col.insert_one({
        "user_id": user.id,
        "name": user.full_name,
        "username": user.username,
        "first_started_at": now(),
        "last_seen_at": now(),
        "blocked": False,
    })

    await update.effective_message.reply_text(WELCOME_TEXT)


@admin_only
async def users(update, context):
    total = await users_col.count_documents({})
    active = await users_col.count_documents({"blocked": {"$ne": True}})
    blocked = await users_col.count_documents({"blocked": True})

    await update.effective_message.reply_text(
        "👥 USER STATISTICS\n\n"
        f"📊 Total Users: {total}\n"
        f"🟢 Active: {active}\n"
        f"🔴 Blocked: {blocked}"
    )


@admin_only
async def chats(update, context):
    total = await chats_col.count_documents({})
    active = await chats_col.count_documents({"enabled": True})
    groups = await chats_col.count_documents({
        "enabled": True,
        "type": {"$in": ["group", "supergroup"]},
    })
    channels = await chats_col.count_documents({
        "enabled": True,
        "type": "channel",
    })

    await update.effective_message.reply_text(
        "📊 CHAT STATISTICS\n\n"
        f"👥 Groups: {groups}\n"
        f"📢 Channels: {channels}\n"
        f"📦 Total Active: {active}\n"
        f"🗃️ Total Records: {total}"
    )


# ============================================================
# ADMIN PANEL / HELP
# ============================================================

def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Users", callback_data="admin_users"),
            InlineKeyboardButton("💬 Chats", callback_data="admin_chats"),
        ],
        [InlineKeyboardButton("🎨 5 Button Sets", callback_data="admin_sets")],
        [InlineKeyboardButton("🔄 Schedules", callback_data="admin_schedules")],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton("👥 User Broadcast", callback_data="admin_ubroadcast"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="admin_close")],
    ])


@admin_only
async def help_command(update, context):
    await update.effective_message.reply_text(
        "🔥 PROMOTION BOT — ADMIN PANEL\n\n"
        "👥 USERS\n/users\n/ubroadcast\n\n"
        "📢 BROADCAST\n/broadcast\n\n"
        "🔄 SCHEDULE\n/schedule\n/schedules\n"
        "/pause ID\n/resume ID\n/stopall\n"
        "/delete_schedule ID\n/deleteall\n\n"
        "🚀 AUTOPOST\n/autopost\n/autoposts\n"
        "/pause_autopost ID\n/resume_autopost ID\n"
        "/delete_autopost ID\n/deleteall_autopost\n\n"
        "🎨 PROMOTION SETS\n/sets\n/setlinks\n/clearsets\n\n"
        "📊 CHATS\n/chats\n\n"
        "❌ /cancel\n\n"
        "🎨 BUTTON COLORS\n"
        "🔵 primary\n🟢 success\n🔴 danger\n\n"
        "📦 5 SETS — 3 BUTTONS EACH\n"
        "📌 Total = 15 names + 15 links\n\n"
        "🔄 SCHEDULE\n"
        "ഓരോ group/channel-ലും വേറെ വേറെ set പോകും.\n"
        "ഓരോ run-ലും set assignment shuffle ചെയ്യും.\n\n"
        "🚀 AUTOPOST\n"
        "ഒരു message എല്ലാ groups/channels-ലേക്കും same ആയി പോകും.\n"
        "Date + Start + End + Interval set ചെയ്യാം."
        ,
        reply_markup=admin_panel_keyboard(),
    )


async def admin_callback(update, context):
    query = update.callback_query
    if not query:
        return

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "admin_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data == "admin_users":
        total = await users_col.count_documents({})
        active = await users_col.count_documents({"blocked": {"$ne": True}})
        blocked = await users_col.count_documents({"blocked": True})
        await query.message.reply_text(
            "👥 USER STATISTICS\n\n"
            f"📊 Total: {total}\n🟢 Active: {active}\n🔴 Blocked: {blocked}"
        )
        return

    if data == "admin_chats":
        total = await chats_col.count_documents({})
        active = await chats_col.count_documents({"enabled": True})
        await query.message.reply_text(
            "💬 CHAT STATISTICS\n\n"
            f"📦 Total Records: {total}\n🟢 Active: {active}"
        )
        return

    if data == "admin_sets":
        await show_sets(query.message, context)
        return

    if data == "admin_schedules":
        rows = await schedules_col.find().sort("created_at", -1).to_list(length=100)
        if not rows:
            await query.message.reply_text("❌ No schedules.")
            return

        lines = ["🔄 SCHEDULES\n"]
        for row in rows:
            status = "🟢 ON" if row.get("enabled") else "⏸ OFF"
            lines.append(
                f"{status}\n"
                f"🆔 {row['_id']}\n"
                f"⏱ Every {row.get('interval_label', '-')}\n"
            )
        await query.message.reply_text("\n".join(lines))
        return

    if data == "admin_broadcast":
        await begin_setup(query.message, context, "broadcast")
        return

    if data == "admin_ubroadcast":
        await begin_setup(query.message, context, "ubroadcast")
        return


# ============================================================
# SETUP / BROADCAST
# ============================================================

async def begin_setup(update, context, mode):
    context.user_data["setup"] = {
        "mode": mode,
        "step": "content",
        "photo_file_id": None,
        "text": "",
        "buttons": [],
    }

    await update.effective_message.reply_text(
        f"🚀 {mode.upper()} SETUP\n\n"
        "🖼️ Send a photo with caption\n"
        "OR\n"
        "📝 Send text\n\n"
        "Then button setup will appear.\n\n"
        "/cancel to cancel."
    )


@admin_only
async def broadcast(update, context):
    await begin_setup(update, context, "broadcast")


@admin_only
async def ubroadcast(update, context):
    await begin_setup(update, context, "ubroadcast")


@admin_only
async def schedule(update, context):
    await begin_setup(update, context, "schedule")


# ============================================================
# NEW: AUTOPOST COMMAND
# ============================================================

@admin_only
async def autopost(update, context):
    await begin_setup(update, context, "autopost")


async def capture_content(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("step") != "content":
        return

    message = update.effective_message

    if message.photo:
        setup["photo_file_id"] = message.photo[-1].file_id
        setup["text"] = message.caption or ""
    elif message.text:
        setup["photo_file_id"] = None
        setup["text"] = message.text
    else:
        return

    # ========================================================
    # NEW AUTOPOST FLOW
    # ========================================================

    if setup["mode"] == "autopost":
        setup["step"] = "button"

        await message.reply_text(
            "🎨 AUTOPOST BUTTON SETUP\n\n"
            "Maximum 3 buttons.\n\n"
            "Format:\n"
            "Button Text | URL | Color\n\n"
            "Example:\n"
            "🎬 Watch | https://example.com | primary\n"
            "👥 Group | https://t.me/group | success\n"
            "📢 Channel | https://t.me/channel | danger\n\n"
            "🔵 primary\n"
            "🟢 success\n"
            "🔴 danger\n\n"
            "Or send: skip"
        )
        return

    # ========================================================
    # OLD SCHEDULE FLOW — UNCHANGED
    # ========================================================

    if setup["mode"] == "schedule":
        setup["step"] = "interval"
        await message.reply_text(
            "⏱️ SELECT INTERVAL\n\n"
            "5m\n10m\n15m\n30m\n1h\n\n"
            "Example: 5m\n\n"
            "🎨 Saved 5 button sets will be distributed "
            "separately across groups/channels."
        )
        return

    setup["step"] = "button"
    await message.reply_text(
        "🎨 MULTI BUTTON SETUP\n\n"
        "Maximum 3 buttons.\n\n"
        "Format:\n"
        "Button Text | URL | Color\n\n"
        "Example:\n"
        "🎬 Watch Video | https://example.com | primary\n"
        "👥 Join Group | https://t.me/group | success\n"
        "📢 Join Channel | https://t.me/channel | danger\n\n"
        "🔵 primary\n🟢 success\n🔴 danger\n\n"
        "Or send: skip"
    )


async def capture_button(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("step") != "button":
        return

    text = (update.effective_message.text or "").strip()

    if text.lower() == "skip":
        setup["buttons"] = []
    else:
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if len(lines) > 3:
            await update.effective_message.reply_text("❌ Maximum 3 buttons മാത്രം.")
            return

        buttons = []
        for line in lines:
            parts = [x.strip() for x in line.split("|")]

            if len(parts) == 2:
                button_text, button_url = parts
                button_style = "primary"
            elif len(parts) == 3:
                button_text, button_url, button_style = parts
                button_style = button_style.lower()
            else:
                await update.effective_message.reply_text(
                    "❌ Invalid format.\nButton Text | URL | Color"
                )
                return

            if not button_text:
                await update.effective_message.reply_text(
                    "❌ Button text cannot be empty."
                )
                return

            if not valid_url(button_url):
                await update.effective_message.reply_text(
                    f"❌ Invalid URL:\n{button_url}"
                )
                return

            if button_style not in ("primary", "success", "danger"):
                await update.effective_message.reply_text(
                    "❌ Invalid color.\nUse primary, success or danger."
                )
                return

            buttons.append({
                "text": button_text[:64],
                "url": button_url,
                "style": button_style,
            })

        setup["buttons"] = buttons

    # ========================================================
    # NEW AUTOPOST FLOW
    # ========================================================

    if setup["mode"] == "autopost":
        setup["step"] = "date"

        await update.effective_message.reply_text(
            "📅 AUTOPOST DATE\n\n"
            "Date അയക്കുക:\n\n"
            "DD/MM/YYYY\n\n"
            "Example:\n"
            "18/09/2026"
        )
        return

    if setup["mode"] == "broadcast":
        await execute_chat_broadcast(update, context)
    else:
        await execute_user_broadcast(update, context)


async def safe_chat_send(bot, chat_id, payload):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await send_payload(bot, chat_id, payload)
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 1)) + 2)
        except (TimedOut, NetworkError):
            if attempt >= MAX_RETRIES:
                raise
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Chat send failed")


async def execute_chat_broadcast(update, context):
    payload = build_payload(context.user_data["setup"])
    rows = await chats_col.find({"enabled": True}).to_list(length=5000)
    total = len(rows)

    if total == 0:
        context.user_data.pop("setup", None)
        await update.effective_message.reply_text("❌ No registered groups/channels.")
        return

    progress = await update.effective_message.reply_text(
        f"📢 BROADCAST STARTING...\n\n📦 Total: {total}\n"
        "✅ Sent: 0\n❌ Failed: 0\n📊 Progress: 0%"
    )

    sent = failed = 0

    for index, row in enumerate(rows, 1):
        try:
            await safe_chat_send(context.bot, row["chat_id"], payload)
            sent += 1
        except Exception as e:
            failed += 1
            log.warning("Broadcast failed chat=%s error=%s", row["chat_id"], e)

        await asyncio.sleep(CHAT_SEND_DELAY)

        if index % PROGRESS_EVERY == 0 or index == total:
            try:
                await progress.edit_text(
                    "📢 BROADCASTING...\n\n"
                    f"📦 Total: {total}\n"
                    f"✅ Sent: {sent}\n"
                    f"⏳ Remaining: {total-index}\n"
                    f"❌ Failed: {failed}\n"
                    f"📊 Progress: {round(index/total*100,1)}%"
                )
            except Exception:
                pass

    context.user_data.pop("setup", None)
    await progress.edit_text(
        "✅ BROADCAST COMPLETED\n\n"
        f"📦 Total: {total}\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


async def safe_user_send(bot, user_id, payload):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await send_payload(bot, user_id, payload)
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 1)) + 2)
        except (TimedOut, NetworkError):
            if attempt >= MAX_RETRIES:
                raise
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Could not send message")


async def execute_user_broadcast(update, context):
    payload = build_payload(context.user_data["setup"])
    total = await users_col.count_documents({"blocked": {"$ne": True}})

    if total == 0:
        context.user_data.pop("setup", None)
        await update.effective_message.reply_text("❌ No users.")
        return

    progress = await update.effective_message.reply_text(
        f"📢 USER BROADCAST STARTING...\n\n👥 Total: {total}\n"
        "✅ Sent: 0\n❌ Failed: 0\n📊 Progress: 0%"
    )

    sent = failed = processed = 0
    cursor = users_col.find(
        {"blocked": {"$ne": True}},
        {"user_id": 1},
    ).sort("_id", 1)

    async for row in cursor:
        user_id = int(row["user_id"])

        try:
            await safe_user_send(context.bot, user_id, payload)
            sent += 1
        except Exception as e:
            failed += 1
            error = str(e).lower()
            if any(x in error for x in (
                "blocked", "deactivated", "chat not found",
                "user is deactivated"
            )):
                await users_col.update_one(
                    {"user_id": user_id},
                    {"$set": {"blocked": True, "blocked_at": now()}},
                )

        processed += 1
        await asyncio.sleep(USER_SEND_DELAY)

        if processed % PROGRESS_EVERY == 0 or processed == total:
            try:
                await progress.edit_text(
                    "📢 USER BROADCASTING...\n\n"
                    f"👥 Total: {total}\n"
                    f"✅ Sent: {sent}\n"
                    f"⏳ Remaining: {max(total-processed,0)}\n"
                    f"❌ Failed: {failed}\n"
                    f"📊 Progress: {round(processed/total*100,1)}%"
                )
            except Exception:
                pass

    context.user_data.pop("setup", None)
    await progress.edit_text(
        "✅ USER BROADCAST COMPLETED\n\n"
        f"👥 Total: {total}\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


# ============================================================
# PROMOTION SETS
# ============================================================

async def get_promo_set(set_no):
    return await sets_col.find_one({"set_no": set_no})


async def save_promo_set(set_no, buttons):
    await sets_col.update_one(
        {"set_no": set_no},
        {
            "$set": {
                "set_no": set_no,
                "buttons": buttons[:3],
                "updated_at": now(),
            },
            "$setOnInsert": {"created_at": now()},
        },
        upsert=True,
    )


async def show_sets(message, context):
    lines = ["🎨 PROMOTION BUTTON SETS", ""]
    complete = 0

    for set_no in SET_NUMBERS:
        row = await get_promo_set(set_no)

        if not row or not row.get("buttons"):
            lines.append(f"❌ SET {set_no} — EMPTY")
            continue

        buttons = row["buttons"]
        if len(buttons) == 3:
            complete += 1

        lines.append(f"✅ SET {set_no} — {len(buttons)}/3 BUTTONS")
        for i, button in enumerate(buttons, 1):
            lines.append(
                f"   {i}. {button.get('text','-')} → "
                f"{button.get('url','-')} [{button.get('style','primary')}]"
            )
        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━",
        f"📦 Completed Sets: {complete}/5",
        "",
        "📝 SET ALL 15 BUTTONS:",
        "/setlinks",
        "",
        "🧹 CLEAR ALL:",
        "/clearsets",
    ])

    await message.reply_text("\n".join(lines))


@admin_only
async def sets_command(update, context):
    await show_sets(update.effective_message, context)


@admin_only
async def setlinks_command(update, context):
    context.user_data["setlinks_mode"] = True
    await update.effective_message.reply_text(
        "🎨 SET 15 BUTTONS\n\n"
        "15 lines അയക്കുക.\n\n"
        "Format:\nName | URL | Color\n\n"
        "📦 Lines 1-3   = SET 1\n"
        "📦 Lines 4-6   = SET 2\n"
        "📦 Lines 7-9   = SET 3\n"
        "📦 Lines 10-12 = SET 4\n"
        "📦 Lines 13-15 = SET 5\n\n"
        "🔵 primary\n🟢 success\n🔴 danger\n\n"
        "⚠️ Exactly 15 lines വേണം.\n"
        "/cancel ഉപയോഗിച്ച് cancel ചെയ്യാം."
    )


async def capture_setlinks(update, context):
    if not context.user_data.get("setlinks_mode"):
        return False

    if not is_admin(update.effective_user.id):
        return True

    message = update.effective_message
    text = (message.text or "").strip()

    if not text:
        return True

    lines = [x.strip() for x in text.splitlines() if x.strip()]

    if len(lines) != 15:
        await message.reply_text(
            "❌ Exactly 15 lines വേണം.\n\n"
            f"നിങ്ങൾ അയച്ചത്: {len(lines)}\n"
            "15 names + 15 links അയക്കുക."
        )
        return True

    all_buttons = []

    for index, line in enumerate(lines, 1):
        parts = [x.strip() for x in line.split("|")]

        if len(parts) == 2:
            button_text, button_url = parts
            button_style = "primary"
        elif len(parts) == 3:
            button_text, button_url, button_style = parts
            button_style = button_style.lower()
        else:
            await message.reply_text(
                f"❌ Invalid line {index}.\nName | URL | Color"
            )
            return True

        if not button_text:
            await message.reply_text(f"❌ Button name empty — Line {index}")
            return True

        if not valid_url(button_url):
            await message.reply_text(
                f"❌ Invalid URL — Line {index}\n{button_url}"
            )
            return True

        if button_style not in ("primary", "success", "danger"):
            await message.reply_text(
                f"❌ Invalid color — Line {index}\n"
                "Use primary, success or danger."
            )
            return True

        all_buttons.append({
            "text": button_text[:64],
            "url": button_url,
            "style": button_style,
        })

    for set_index in range(5):
        await save_promo_set(
            set_index + 1,
            all_buttons[set_index * 3:(set_index + 1) * 3],
        )

    context.user_data.pop("setlinks_mode", None)

    await message.reply_text(
        "✅ ALL 5 SETS SAVED!\n\n"
        "📦 SET 1 → 3 buttons\n"
        "📦 SET 2 → 3 buttons\n"
        "📦 SET 3 → 3 buttons\n"
        "📦 SET 4 → 3 buttons\n"
        "📦 SET 5 → 3 buttons\n\n"
        "🎯 Total: 15 Names + 15 Links\n\n"
        "🔄 Schedule ഓരോ run-ലും groups/channels-ന് "
        "വേറെ വേറെ sets നൽകും.\n\n"
        "👉 /sets"
    )
    return True


@admin_only
async def clearsets_command(update, context):
    await sets_col.delete_many({})
    await update.effective_message.reply_text(
        "🧹 ALL PROMOTION SETS CLEARED.\n\n"
        "5 sets are now empty."
    )


# ============================================================
# OLD SCHEDULE — UNCHANGED
# ============================================================

async def get_set_payload(set_no, base_payload):
    row = await get_promo_set(set_no)
    buttons = row.get("buttons", []) if row else []

    return {
        "photo_file_id": base_payload.get("photo_file_id"),
        "text": base_payload.get("text") or "",
        "buttons": buttons[:3],
    }


def create_rotation_order():
    order = SET_NUMBERS.copy()
    random.shuffle(order)
    return order


async def capture_interval(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("step") != "interval":
        return

    value = (update.effective_message.text or "").strip().lower()

    if value not in INTERVALS:
        await update.effective_message.reply_text(
            "❌ Invalid interval.\n\n5m\n10m\n15m\n30m\n1h"
        )
        return

    configured = 0
    for set_no in SET_NUMBERS:
        row = await get_promo_set(set_no)
        if row and len(row.get("buttons", [])) == 3:
            configured += 1

    if configured < 5:
        await update.effective_message.reply_text(
            "❌ 5 Button Sets complete ചെയ്തിട്ടില്ല.\n\n"
            f"Configured: {configured}/5\n\n"
            "ആദ്യം /setlinks ഉപയോഗിച്ച് 15 names + 15 links അയക്കുക."
        )
        return

    base_payload = build_payload(setup)

    doc = {
        "interval": INTERVALS[value],
        "interval_label": value,
        "photo_file_id": base_payload.get("photo_file_id"),
        "text": base_payload.get("text") or "",
        "payload": base_payload,
        "enabled": True,
        "last_messages": {},
        "created_at": now(),
        "updated_at": now(),
    }

    result = await schedules_col.insert_one(doc)
    schedule_id = str(result.inserted_id)

    await run_schedule(schedule_id, context.application)

    context.application.job_queue.run_repeating(
        scheduled_job,
        interval=INTERVALS[value],
        first=INTERVALS[value],
        data={"schedule_id": schedule_id},
        name=f"schedule:{schedule_id}",
    )

    context.user_data.pop("setup", None)

    await update.effective_message.reply_text(
        "✅ SCHEDULE CREATED\n\n"
        f"🆔 {schedule_id}\n"
        f"⏱ Every {value}\n\n"
        "🎨 5 Button Sets Active\n"
        "📦 5 × 3 = 15 links\n\n"
        "🔀 ഓരോ group/channel-നും വേറെ set.\n"
        "🔄 ഓരോ run-ലും assignment shuffle ചെയ്യും.\n\n"
        f"🗑 Delete:\n/delete_schedule {schedule_id}"
    )


async def delete_message_safe(bot, chat_id, message_id):
    if not message_id:
        return False

    for attempt in range(MAX_RETRIES + 1):
        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
            return True
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 1)) + 2)
        except (TimedOut, NetworkError):
            if attempt >= MAX_RETRIES:
                return False
            await asyncio.sleep(2 ** attempt)
        except Exception:
            return False

    return False


async def run_schedule(schedule_id, application):
    try:
        object_id = ObjectId(schedule_id)
    except Exception:
        log.error("Invalid schedule ID: %s", schedule_id)
        return

    doc = await schedules_col.find_one({
        "_id": object_id,
        "enabled": True,
    })

    if not doc:
        return

    rows = await chats_col.find({
        "enabled": True
    }).to_list(length=5000)

    if not rows:
        log.info("SCHEDULE %s: No active chats.", schedule_id)
        return

    random.shuffle(rows)

    set_order = SET_NUMBERS.copy()
    random.shuffle(set_order)

    log.info(
        "SCHEDULE %s | CHAT COUNT=%d | SET ORDER=%s",
        schedule_id,
        len(rows),
        set_order,
    )

    base_payload = {
        "photo_file_id": doc.get("photo_file_id"),
        "text": doc.get("text") or "",
    }

    old_payload = doc.get("payload", {})
    if not base_payload["text"]:
        base_payload["text"] = old_payload.get("text") or ""

    if not base_payload["photo_file_id"]:
        base_payload["photo_file_id"] = old_payload.get("photo_file_id")

    old_messages = doc.get("last_messages", {})
    new_messages = {}

    for index, row in enumerate(rows):
        chat_id = row["chat_id"]
        key = str(chat_id)

        set_no = set_order[index % 5]

        payload = await get_set_payload(
            set_no,
            base_payload,
        )

        old_message_id = old_messages.get(key)

        if old_message_id:
            await delete_message_safe(
                application.bot,
                chat_id,
                old_message_id,
            )

        try:
            message = await safe_chat_send(
                application.bot,
                chat_id,
                payload,
            )

            new_messages[key] = message.message_id

            log.info(
                "SCHEDULE %s | CHAT %s | SET %s | SENT",
                schedule_id,
                chat_id,
                set_no,
            )

        except Exception as e:
            log.warning(
                "Schedule failed | chat=%s | set=%s | error=%s",
                chat_id,
                set_no,
                e,
            )

        await asyncio.sleep(CHAT_SEND_DELAY)

    await schedules_col.update_one(
        {"_id": object_id},
        {
            "$set": {
                "last_messages": new_messages,
                "last_set_order": set_order,
                "last_run_at": now(),
                "updated_at": now(),
            }
        },
    )

    log.info(
        "SCHEDULE %s COMPLETED | SET ORDER=%s",
        schedule_id,
        set_order,
    )


async def scheduled_job(context):
    schedule_id = (context.job.data or {}).get("schedule_id")
    if not schedule_id:
        return

    await run_schedule(
        schedule_id,
        context.application,
    )


async def restore_schedules(application):
    rows = await schedules_col.find({"enabled": True}).to_list(length=1000)
    restored = 0

    for row in rows:
        schedule_id = str(row["_id"])

        for job in application.job_queue.get_jobs_by_name(
            f"schedule:{schedule_id}"
        ):
            job.schedule_removal()

        interval = int(row.get("interval", 300))

        application.job_queue.run_repeating(
            scheduled_job,
            interval=interval,
            first=interval,
            data={"schedule_id": schedule_id},
            name=f"schedule:{schedule_id}",
        )
        restored += 1

    log.info("Restored %d schedules", restored)


@admin_only
async def schedules(update, context):
    rows = await schedules_col.find().sort("created_at", -1).to_list(length=100)

    if not rows:
        await update.effective_message.reply_text("❌ No schedules.")
        return

    lines = ["🔄 SCHEDULES\n"]

    for row in rows:
        status = "🟢 ON" if row.get("enabled") else "⏸ OFF"
        lines.append(
            f"{status}\n"
            f"🆔 {row['_id']}\n"
            f"⏱ Every {row.get('interval_label', '-')}\n"
        )

    await update.effective_message.reply_text("\n".join(lines))


@admin_only
async def pause(update, context):
    if not context.args:
        await update.effective_message.reply_text("/pause <schedule_id>")
        return

    sid = context.args[0]

    try:
        object_id = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text("❌ Invalid ID.")
        return

    result = await schedules_col.update_one(
        {"_id": object_id},
        {"$set": {"enabled": False, "updated_at": now()}},
    )

    for job in context.application.job_queue.get_jobs_by_name(
        f"schedule:{sid}"
    ):
        job.schedule_removal()

    await update.effective_message.reply_text(
        "⏸ Schedule paused." if result.matched_count
        else "❌ Schedule not found."
    )


@admin_only
async def resume(update, context):
    if not context.args:
        await update.effective_message.reply_text("/resume <schedule_id>")
        return

    sid = context.args[0]

    try:
        object_id = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text("❌ Invalid ID.")
        return

    row = await schedules_col.find_one({"_id": object_id})

    if not row:
        await update.effective_message.reply_text("❌ Schedule not found.")
        return

    await schedules_col.update_one(
        {"_id": object_id},
        {"$set": {"enabled": True, "updated_at": now()}},
    )

    for job in context.application.job_queue.get_jobs_by_name(
        f"schedule:{sid}"
    ):
        job.schedule_removal()

    context.application.job_queue.run_repeating(
        scheduled_job,
        interval=int(row.get("interval", 300)),
        first=1,
        data={"schedule_id": sid},
        name=f"schedule:{sid}",
    )

    await update.effective_message.reply_text("▶️ Schedule resumed.")


@admin_only
async def stop_all_schedules(update, context):
    result = await schedules_col.update_many(
        {"enabled": True},
        {"$set": {"enabled": False, "updated_at": now()}},
    )

    removed = 0

    for job in context.application.job_queue.jobs():
        if job.name and job.name.startswith("schedule:"):
            job.schedule_removal()
            removed += 1

    if result.modified_count == 0 and removed == 0:
        await update.effective_message.reply_text(
            "ℹ️ NO ACTIVE SCHEDULES"
        )
        return

    await update.effective_message.reply_text(
        "🛑 ALL SCHEDULES STOPPED\n\n"
        f"🔴 Stopped: {result.modified_count}\n"
        f"⏹️ Jobs removed: {removed}\n\n"
        "💾 Schedules are still saved.\n"
        "▶️ Use /resume ID to start one again."
    )


@admin_only
async def delete_schedule(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "/delete_schedule <schedule_id>"
        )
        return

    sid = context.args[0]

    try:
        object_id = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text("❌ Invalid ID.")
        return

    row = await schedules_col.find_one({"_id": object_id})

    if not row:
        await update.effective_message.reply_text("❌ Schedule not found.")
        return

    deleted = failed = 0

    for chat_id, message_id in row.get("last_messages", {}).items():
        if await delete_message_safe(
            context.bot,
            int(chat_id),
            int(message_id),
        ):
            deleted += 1
        else:
            failed += 1

    for job in context.application.job_queue.get_jobs_by_name(
        f"schedule:{sid}"
    ):
        job.schedule_removal()

    await schedules_col.delete_one({"_id": object_id})

    await update.effective_message.reply_text(
        "🗑 SCHEDULE DELETED\n\n"
        f"🆔 {sid}\n"
        f"✅ Messages deleted: {deleted}\n"
        f"❌ Failed: {failed}"
    )


@admin_only
async def delete_all_schedule_messages(update, context):
    rows = await schedules_col.find({}).to_list(length=1000)

    if not rows:
        await update.effective_message.reply_text("ℹ️ No schedules found.")
        return

    total_messages = deleted = failed = 0

    for row in rows:
        last_messages = row.get("last_messages", {})
        total_messages += len(last_messages)

        for chat_id, message_id in last_messages.items():
            if await delete_message_safe(
                context.bot,
                int(chat_id),
                int(message_id),
            ):
                deleted += 1
            else:
                failed += 1

        await schedules_col.update_one(
            {"_id": row["_id"]},
            {"$set": {"last_messages": {}, "updated_at": now()}},
        )

    await update.effective_message.reply_text(
        "🧹 ALL SCHEDULE MESSAGES PROCESSED\n\n"
        f"📦 Tracked messages: {total_messages}\n"
        f"✅ Deleted: {deleted}\n"
        f"❌ Failed: {failed}\n\n"
        "💾 Schedule settings are still saved."
    )


# ============================================================
# NEW AUTOPOST
# ============================================================

def parse_autopost_date(value):
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_autopost_time(value):
    value = value.strip()

    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(value.upper(), fmt).time()
        except ValueError:
            pass

    return None


def make_autopost_window(run_date, start_time, end_time):
    start_dt = datetime.combine(
        run_date,
        start_time,
        tzinfo=IST,
    )

    end_dt = datetime.combine(
        run_date,
        end_time,
        tzinfo=IST,
    )

    # Example:
    # 22:30 -> 00:00
    # End is treated as next day.
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    return start_dt, end_dt


def calculate_first_autopost_run(start_dt, end_dt, interval_seconds):
    current = ist_now()

    if current < start_dt:
        return start_dt

    if current > end_dt:
        return None

    elapsed = (current - start_dt).total_seconds()

    if elapsed <= 0:
        return start_dt

    steps = int(elapsed // interval_seconds)

    candidate = start_dt + timedelta(
        seconds=steps * interval_seconds
    )

    if candidate < current:
        candidate += timedelta(seconds=interval_seconds)

    if candidate > end_dt:
        return None

    return candidate


async def capture_autopost_date(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("mode") != "autopost":
        return

    value = (update.effective_message.text or "").strip()
    run_date = parse_autopost_date(value)

    if not run_date:
        await update.effective_message.reply_text(
            "❌ Invalid date.\n\n"
            "Use:\n"
            "DD/MM/YYYY\n\n"
            "Example:\n"
            "18/09/2026"
        )
        return

    today = ist_now().date()

    if run_date < today:
        await update.effective_message.reply_text(
            "❌ Past date അനുവദിക്കില്ല.\n\n"
            "Future date നൽകുക."
        )
        return

    setup["run_date"] = run_date.isoformat()
    setup["step"] = "start_time"

    await update.effective_message.reply_text(
        "🕙 START TIME\n\n"
        "Time അയക്കുക:\n\n"
        "24-hour format:\n"
        "HH:MM\n\n"
        "Example:\n"
        "22:30\n\n"
        "🌙 Night example: 10:30 PM = 22:30"
    )


async def capture_autopost_start_time(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("mode") != "autopost":
        return

    value = (update.effective_message.text or "").strip()
    start_time = parse_autopost_time(value)

    if not start_time:
        await update.effective_message.reply_text(
            "❌ Invalid time.\n\n"
            "Use HH:MM\n"
            "Example: 22:30"
        )
        return

    setup["start_time"] = start_time.strftime("%H:%M")
    setup["step"] = "end_time"

    await update.effective_message.reply_text(
        "🕛 END TIME\n\n"
        "Time അയക്കുക:\n\n"
        "HH:MM\n\n"
        "Example:\n"
        "00:00\n\n"
        "22:30 → 00:00 കൊടുത്താൽ\n"
        "അത് അടുത്ത ദിവസത്തെ 00:00 ആയി കണക്കാക്കും."
    )


async def capture_autopost_end_time(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("mode") != "autopost":
        return

    value = (update.effective_message.text or "").strip()
    end_time = parse_autopost_time(value)

    if not end_time:
        await update.effective_message.reply_text(
            "❌ Invalid time.\n\n"
            "Use HH:MM\n"
            "Example: 00:00"
        )
        return

    start_time = datetime.strptime(
        setup["start_time"],
        "%H:%M",
    ).time()

    run_date = datetime.strptime(
        setup["run_date"],
        "%Y-%m-%d",
    ).date()

    start_dt, end_dt = make_autopost_window(
        run_date,
        start_time,
        end_time,
    )

    if end_dt <= start_dt:
        await update.effective_message.reply_text(
            "❌ Invalid time range."
        )
        return

    if end_dt <= ist_now():
        await update.effective_message.reply_text(
            "❌ ഈ schedule സമയം കഴിഞ്ഞതാണ്.\n\n"
            "Future time നൽകുക."
        )
        return

    setup["end_time"] = end_time.strftime("%H:%M")
    setup["step"] = "autopost_interval"

    await update.effective_message.reply_text(
        "⏱️ INTERVAL\n\n"
        "എത്ര minutes ഇടവിട്ട് message അയക്കണം?\n\n"
        "Number മാത്രം അയക്കുക.\n\n"
        "Example:\n"
        "2\n\n"
        "അതായത് ഓരോ 2 minutes-ലും.\n\n"
        "Minimum: 1 minute"
    )


async def capture_autopost_interval(update, context):
    setup = context.user_data.get("setup")
    if not setup or setup.get("mode") != "autopost":
        return

    value = (update.effective_message.text or "").strip()

    try:
        minutes = int(value)
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Number മാത്രം നൽകുക.\n\n"
            "Example: 2"
        )
        return

    if minutes < 1 or minutes > AUTOPOST_MAX_INTERVAL:
        await update.effective_message.reply_text(
            f"❌ Interval 1 മുതൽ {AUTOPOST_MAX_INTERVAL} minutes വരെ മാത്രം."
        )
        return

    run_date = datetime.strptime(
        setup["run_date"],
        "%Y-%m-%d",
    ).date()

    start_time = datetime.strptime(
        setup["start_time"],
        "%H:%M",
    ).time()

    end_time = datetime.strptime(
        setup["end_time"],
        "%H:%M",
    ).time()

    start_dt, end_dt = make_autopost_window(
        run_date,
        start_time,
        end_time,
    )

    interval_seconds = minutes * 60

    first_run = calculate_first_autopost_run(
        start_dt,
        end_dt,
        interval_seconds,
    )

    if not first_run:
        await update.effective_message.reply_text(
            "❌ ഈ schedule-ന്റെ സമയം കഴിഞ്ഞു."
        )
        context.user_data.pop("setup", None)
        return

    payload = build_payload(setup)

    doc = {
        "photo_file_id": payload.get("photo_file_id"),
        "text": payload.get("text") or "",
        "payload": payload,
        "buttons": payload.get("buttons", []),

        "run_date": setup["run_date"],
        "start_time": setup["start_time"],
        "end_time": setup["end_time"],

        "interval_minutes": minutes,
        "interval_seconds": interval_seconds,
        "interval_label": f"{minutes}m",

        "start_at": start_dt.astimezone(timezone.utc),
        "end_at": end_dt.astimezone(timezone.utc),
        "next_run_at": first_run.astimezone(timezone.utc),

        "enabled": True,
        "status": "scheduled",

        "last_messages": {},
        "last_run_at": None,

        "created_at": now(),
        "updated_at": now(),
    }

    result = await autoposts_col.insert_one(doc)
    autopost_id = str(result.inserted_id)

    context.user_data.pop("setup", None)

    await update.effective_message.reply_text(
        "✅ AUTOPOST CREATED\n\n"
        f"🆔 {autopost_id}\n"
        f"📅 Date: {run_date.strftime('%d/%m/%Y')}\n"
        f"🕙 Start: {setup['start_time']}\n"
        f"🕛 End: {setup['end_time']}\n"
        f"⏱ Interval: {minutes} minute(s)\n\n"
        "📢 എല്ലാ registered groups/channels-ലും\n"
        "SAME message ആയിരിക്കും പോകുക.\n\n"
        "🗑️ പുതിയ message അയക്കുന്നതിന് മുമ്പ്\n"
        "ആ chat-ലെ previous autopost message delete ചെയ്യും.\n\n"
        "💾 MongoDB-യിൽ saved.\n"
        "🔄 Bot restart ചെയ്താലും തുടരും.\n\n"
        f"🆔 ID: {autopost_id}"
    )


async def run_autopost(autopost_id, application):
    try:
        object_id = ObjectId(autopost_id)
    except Exception:
        log.error("Invalid autopost ID: %s", autopost_id)
        return

    doc = await autoposts_col.find_one({
        "_id": object_id,
        "enabled": True,
    })

    if not doc:
        return

    payload = doc.get("payload") or {
        "photo_file_id": doc.get("photo_file_id"),
        "text": doc.get("text") or "",
        "buttons": doc.get("buttons") or [],
    }

    rows = await chats_col.find({
        "enabled": True,
        "type": {"$in": ["group", "supergroup", "channel"]},
    }).to_list(length=5000)

    if not rows:
        log.info(
            "AUTOPOST %s: No active groups/channels.",
            autopost_id,
        )
        return

    old_messages = doc.get("last_messages", {})
    new_messages = {}

    sent = 0
    failed = 0

    log.info(
        "AUTOPOST %s | Sending SAME message to %d chats",
        autopost_id,
        len(rows),
    )

    for row in rows:
        chat_id = row["chat_id"]
        key = str(chat_id)

        # ----------------------------------------------------
        # DELETE PREVIOUS AUTOPOST MESSAGE
        # ----------------------------------------------------

        old_message_id = old_messages.get(key)

        if old_message_id:
            await delete_message_safe(
                application.bot,
                chat_id,
                old_message_id,
            )

        # ----------------------------------------------------
        # SEND SAME MESSAGE
        # ----------------------------------------------------

        try:
            message = await safe_chat_send(
                application.bot,
                chat_id,
                payload,
            )

            new_messages[key] = message.message_id
            sent += 1

            log.info(
                "AUTOPOST %s | chat=%s | SENT",
                autopost_id,
                chat_id,
            )

        except Exception as e:
            failed += 1

            log.warning(
                "AUTOPOST failed | id=%s | chat=%s | error=%s",
                autopost_id,
                chat_id,
                e,
            )

        await asyncio.sleep(CHAT_SEND_DELAY)

    await autoposts_col.update_one(
        {"_id": object_id},
        {
            "$set": {
                "last_messages": new_messages,
                "last_run_at": now(),
                "updated_at": now(),
            }
        },
    )

    log.info(
        "AUTOPOST %s completed | sent=%d | failed=%d",
        autopost_id,
        sent,
        failed,
    )


async def autopost_scheduler_job(context):
    current = now()

    rows = await autoposts_col.find({
        "enabled": True,
        "status": {"$in": ["scheduled", "running"]},
        "next_run_at": {"$lte": current},
    }).to_list(length=100)

    for row in rows:
        autopost_id = str(row["_id"])

        # ----------------------------------------------------
        # Atomic claim.
        #
        # Prevents the same schedule from being picked twice
        # at the same moment.
        # ----------------------------------------------------

        claim_until = current + timedelta(minutes=10)

        claimed = await autoposts_col.find_one_and_update(
            {
                "_id": row["_id"],
                "enabled": True,
                "status": {"$in": ["scheduled", "running"]},
                "next_run_at": {"$lte": current},
                "$or": [
                    {"lease_until": {"$exists": False}},
                    {"lease_until": {"$lte": current}},
                ],
            },
            {
                "$set": {
                    "status": "running",
                    "lease_until": claim_until,
                    "updated_at": now(),
                }
            },
        )

        if not claimed:
            continue

        try:
            await run_autopost(
                autopost_id,
                context.application,
            )

            fresh = await autoposts_col.find_one({
                "_id": row["_id"],
            })

            if not fresh:
                continue

            old_next = fresh.get("next_run_at")
            end_at = fresh.get("end_at")

            if not old_next or not end_at:
                await autoposts_col.update_one(
                    {"_id": row["_id"]},
                    {
                        "$set": {
                            "enabled": False,
                            "status": "completed",
                            "completed_at": now(),
                            "updated_at": now(),
                        },
                        "$unset": {
                            "lease_until": "",
                        },
                    },
                )
                continue

            next_run = old_next + timedelta(
                seconds=int(
                    fresh.get(
                        "interval_seconds",
                        int(fresh.get("interval_minutes", 1)) * 60,
                    )
                )
            )

            # ------------------------------------------------
            # END TIME INCLUDED
            #
            # Example:
            # 22:30 to 00:00 every 2 min
            #
            # 22:30
            # 22:32
            # ...
            # 23:58
            # 00:00
            # ------------------------------------------------

            if next_run <= end_at:
                await autoposts_col.update_one(
                    {"_id": row["_id"]},
                    {
                        "$set": {
                            "next_run_at": next_run,
                            "status": "scheduled",
                            "updated_at": now(),
                        },
                        "$unset": {
                            "lease_until": "",
                        },
                    },
                )
            else:
                await autoposts_col.update_one(
                    {"_id": row["_id"]},
                    {
                        "$set": {
                            "enabled": False,
                            "status": "completed",
                            "completed_at": now(),
                            "updated_at": now(),
                        },
                        "$unset": {
                            "lease_until": "",
                        },
                    },
                )

        except Exception as e:
            log.error(
                "AUTOPOST scheduler error id=%s error=%s",
                autopost_id,
                e,
                exc_info=True,
            )

            await autoposts_col.update_one(
                {"_id": row["_id"]},
                {
                    "$set": {
                        "status": "scheduled",
                        "lease_until": now(),
                        "updated_at": now(),
                    }
                },
            )


async def restore_autopost_scheduler(application):
    # One persistent polling job handles all future autoposts.
    for job in application.job_queue.get_jobs_by_name(
        "autopost:master"
    ):
        job.schedule_removal()

    application.job_queue.run_repeating(
        autopost_scheduler_job,
        interval=AUTPOST_CHECK_INTERVAL,
        first=5,
        name="autopost:master",
    )

    count = await autoposts_col.count_documents({
        "enabled": True,
    })

    log.info(
        "Autopost scheduler restored | active=%d",
        count,
    )


@admin_only
async def autoposts(update, context):
    rows = await autoposts_col.find().sort(
        "created_at",
        -1,
    ).to_list(length=100)

    if not rows:
        await update.effective_message.reply_text(
            "❌ No autoposts."
        )
        return

    lines = ["🚀 AUTOPOSTS\n"]

    for row in rows:
        enabled = row.get("enabled", False)
        status = row.get("status", "-")

        if enabled:
            status_text = "🟢 ON"
        elif status == "completed":
            status_text = "✅ COMPLETED"
        else:
            status_text = "⏸ OFF"

        lines.append(
            f"{status_text}\n"
            f"🆔 {row['_id']}\n"
            f"📅 {row.get('run_date', '-')}\n"
            f"🕙 {row.get('start_time', '-')}"
            f" → {row.get('end_time', '-')}\n"
            f"⏱ Every {row.get('interval_label', '-')}\n"
            f"📌 Status: {status}\n"
        )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


@admin_only
async def pause_autopost(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "/pause_autopost <autopost_id>"
        )
        return

    sid = context.args[0]

    try:
        object_id = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text(
            "❌ Invalid ID."
        )
        return

    result = await autoposts_col.update_one(
        {"_id": object_id},
        {
            "$set": {
                "enabled": False,
                "status": "paused",
                "updated_at": now(),
            },
            "$unset": {
                "lease_until": "",
            },
        },
    )

    await update.effective_message.reply_text(
        "⏸️ Autopost paused."
        if result.matched_count
        else "❌ Autopost not found."
    )


@admin_only
async def resume_autopost(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "/resume_autopost <autopost_id>"
        )
        return

    sid = context.args[0]

    try:
        object_id = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text(
            "❌ Invalid ID."
        )
        return

    row = await autoposts_col.find_one({
        "_id": object_id,
    })

    if not row:
        await update.effective_message.reply_text(
            "❌ Autopost not found."
        )
        return

    if row.get("status") == "completed":
        await update.effective_message.reply_text(
            "❌ This autopost is already completed.\n\n"
            "Create a new /autopost for another date."
        )
        return

    current = now()
    next_run = row.get("next_run_at")
    end_at = row.get("end_at")

    if not next_run or not end_at:
        await update.effective_message.reply_text(
            "❌ Schedule timing information missing."
        )
        return

    if next_run < current:
        interval_seconds = int(
            row.get(
                "interval_seconds",
                int(row.get("interval_minutes", 1)) * 60,
            )
        )

        while next_run < current:
            next_run += timedelta(
                seconds=interval_seconds
            )

        if next_run > end_at:
            await autoposts_col.update_one(
                {"_id": object_id},
                {
                    "$set": {
                        "enabled": False,
                        "status": "completed",
                        "completed_at": now(),
                        "updated_at": now(),
                    }
                },
            )

            await update.effective_message.reply_text(
                "❌ Autopost time window is already over."
            )
            return

    await autoposts_col.update_one(
        {"_id": object_id},
        {
            "$set": {
                "enabled": True,
                "status": "scheduled",
                "next_run_at": next_run,
                "updated_at": now(),
            },
            "$unset": {
                "lease_until": "",
            },
        },
    )

    await update.effective_message.reply_text(
        "▶️ Autopost resumed."
    )


@admin_only
async def delete_autopost(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "/delete_autopost <autopost_id>"
        )
        return

    sid = context.args[0]

    try:
        object_id = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text(
            "❌ Invalid ID."
        )
        return

    row = await autoposts_col.find_one({
        "_id": object_id,
    })

    if not row:
        await update.effective_message.reply_text(
            "❌ Autopost not found."
        )
        return

    deleted = failed = 0

    for chat_id, message_id in row.get(
        "last_messages",
        {},
    ).items():

        if await delete_message_safe(
            context.bot,
            int(chat_id),
            int(message_id),
        ):
            deleted += 1
        else:
            failed += 1

    await autoposts_col.delete_one({
        "_id": object_id,
    })

    await update.effective_message.reply_text(
        "🗑️ AUTOPOST DELETED\n\n"
        f"🆔 {sid}\n"
        f"✅ Messages deleted: {deleted}\n"
        f"❌ Failed: {failed}"
    )


@admin_only
async def delete_all_autopost_messages(update, context):
    rows = await autoposts_col.find({}).to_list(
        length=1000
    )

    if not rows:
        await update.effective_message.reply_text(
            "ℹ️ No autoposts found."
        )
        return

    total = deleted = failed = 0

    for row in rows:
        last_messages = row.get(
            "last_messages",
            {},
        )

        total += len(last_messages)

        for chat_id, message_id in last_messages.items():
            if await delete_message_safe(
                context.bot,
                int(chat_id),
                int(message_id),
            ):
                deleted += 1
            else:
                failed += 1

        await autoposts_col.update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "last_messages": {},
                    "updated_at": now(),
                }
            },
        )

    await update.effective_message.reply_text(
        "🧹 ALL AUTOPOST MESSAGES PROCESSED\n\n"
        f"📦 Tracked messages: {total}\n"
        f"✅ Deleted: {deleted}\n"
        f"❌ Failed: {failed}\n\n"
        "💾 Autopost schedules are still saved."
    )


# ============================================================
# CANCEL
# ============================================================

@admin_only
async def cancel(update, context):
    context.user_data.pop("setup", None)
    context.user_data.pop("setlinks_mode", None)
    await update.effective_message.reply_text("❌ Cancelled.")


# ============================================================
# SETUP ROUTER
# ============================================================

async def setup_router(update, context):
    if context.user_data.get("setlinks_mode"):
        await capture_setlinks(update, context)
        return

    setup = context.user_data.get("setup")
    if not setup:
        return

    step = setup.get("step")

    if step == "content":
        await capture_content(update, context)

    elif step == "button":
        await capture_button(update, context)

    elif step == "interval":
        await capture_interval(update, context)

    # ========================================================
    # NEW AUTOPOST ROUTES
    # ========================================================

    elif step == "date":
        await capture_autopost_date(update, context)

    elif step == "start_time":
        await capture_autopost_start_time(update, context)

    elif step == "end_time":
        await capture_autopost_end_time(update, context)

    elif step == "autopost_interval":
        await capture_autopost_interval(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    log.error(
        "Unhandled error: %s",
        context.error,
        exc_info=True,
    )


# ============================================================
# DATABASE INIT
# ============================================================

async def post_init(application):
    global mongo, db
    global users_col, chats_col
    global schedules_col, sets_col
    global autoposts_col

    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing.")

    mongo = AsyncIOMotorClient(MONGO_URI)
    db = mongo[DB_NAME]

    users_col = db["users"]
    chats_col = db["chats"]
    schedules_col = db["schedules"]
    sets_col = db["promo_sets"]

    # NEW COLLECTION
    autoposts_col = db["autoposts"]

    await users_col.create_index(
        "user_id",
        unique=True,
    )

    await chats_col.create_index(
        "chat_id",
        unique=True,
    )

    await schedules_col.create_index(
        "enabled"
    )

    await sets_col.create_index(
        "set_no",
        unique=True,
    )

    # NEW AUTOPOST INDEXES
    await autoposts_col.create_index(
        "enabled"
    )

    await autoposts_col.create_index(
        "next_run_at"
    )

    await autoposts_col.create_index(
        [
            ("enabled", 1),
            ("next_run_at", 1),
        ]
    )

    # OLD SCHEDULES
    await restore_schedules(application)

    # NEW AUTOPOST SCHEDULER
    await restore_autopost_scheduler(application)

    log.info("Database initialized.")


async def post_shutdown(application):
    global mongo
    if mongo:
        mongo.close()
        log.info("MongoDB connection closed.")


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is missing.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("users", users)
    )

    application.add_handler(
        CommandHandler("chats", chats)
    )

    application.add_handler(
        CommandHandler("broadcast", broadcast)
    )

    application.add_handler(
        CommandHandler("ubroadcast", ubroadcast)
    )

    # OLD SCHEDULE — UNCHANGED
    application.add_handler(
        CommandHandler("schedule", schedule)
    )

    application.add_handler(
        CommandHandler("schedules", schedules)
    )

    application.add_handler(
        CommandHandler("pause", pause)
    )

    application.add_handler(
        CommandHandler("resume", resume)
    )

    application.add_handler(
        CommandHandler("stopall", stop_all_schedules)
    )

    application.add_handler(
        CommandHandler(
            "delete_schedule",
            delete_schedule,
        )
    )

    application.add_handler(
        CommandHandler(
            "deleteall",
            delete_all_schedule_messages,
        )
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    # ========================================================
    # NEW AUTOPOST COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler("autopost", autopost)
    )

    application.add_handler(
        CommandHandler("autoposts", autoposts)
    )

    application.add_handler(
        CommandHandler(
            "pause_autopost",
            pause_autopost,
        )
    )

    application.add_handler(
        CommandHandler(
            "resume_autopost",
            resume_autopost,
        )
    )

    application.add_handler(
        CommandHandler(
            "delete_autopost",
            delete_autopost,
        )
    )

    application.add_handler(
        CommandHandler(
            "deleteall_autopost",
            delete_all_autopost_messages,
        )
    )

    # ========================================================
    # PROMOTION SETS
    # ========================================================

    application.add_handler(
        CommandHandler("sets", sets_command)
    )

    application.add_handler(
        CommandHandler("setlinks", setlinks_command)
    )

    application.add_handler(
        CommandHandler("clearsets", clearsets_command)
    )

    # ========================================================
    # ADMIN PANEL
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_",
        )
    )

    # ========================================================
    # CHAT REGISTRATION
    # ========================================================

    application.add_handler(
        ChatMemberHandler(
            my_chat_member_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    application.add_handler(
        ChatMemberHandler(
            chat_member_update,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # ========================================================
    # SETUP TEXT / PHOTO
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
            setup_router,
        )
    )

    application.add_error_handler(
        error_handler
    )

    log.info(
        "Starting promotion bot..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
