import os
import asyncio
import logging
import random
from datetime import datetime, timezone
from functools import wraps

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.constants import ChatMemberStatus

from telegram.error import (
    RetryAfter,
    TimedOut,
    NetworkError,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("promotion-bot")


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "promotion_bot")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}


# ============================================================
# RATE LIMIT
# ============================================================

CHAT_SEND_DELAY = 1.2
USER_SEND_DELAY = 1.5

PROGRESS_EVERY = 10
MAX_RETRIES = 3


# ============================================================
# WELCOME
# ============================================================

WELCOME_TEXT = (
    "👋 Welcome!\n\n"
    "🎬 പുതിയ വീഡിയോകൾ\n"
    "👥 നല്ല ഗ്രൂപ്പുകൾ\n"
    "📢 പുതിയ ചാനലുകൾ\n\n"
    "എല്ലാം ഇവിടെ ലഭിക്കും ❤️"
)


# ============================================================
# DATABASE
# ============================================================

mongo = None
db = None

users_col = None
chats_col = None
schedules_col = None
sets_col = None


def now():
    return datetime.now(timezone.utc)


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):

    @wraps(func)
    async def wrapper(update, context):

        user = update.effective_user

        if not user or not is_admin(user.id):

            if update.effective_message:
                await update.effective_message.reply_text(
                    "⛔ Admin only."
                )

            return

        return await func(update, context)

    return wrapper


# ============================================================
# PAYLOAD
# ============================================================

def build_payload(setup):

    buttons = setup.get("buttons")

    if buttons is None:

        old_text = setup.get("button_text")
        old_url = setup.get("button_url")

        buttons = []

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


# ============================================================
# KEYBOARD
# ============================================================

def build_keyboard(payload):

    buttons = payload.get("buttons") or []

    if not buttons:
        return None

    keyboard = []

    for button in buttons[:3]:

        button_text = (
            button.get("text") or ""
        ).strip()

        button_url = (
            button.get("url") or ""
        ).strip()

        button_style = (
            button.get("style")
            or "primary"
        ).lower().strip()

        if not button_text or not button_url:
            continue

        if button_style not in (
            "primary",
            "success",
            "danger",
        ):
            button_style = "primary"

        keyboard.append([
            InlineKeyboardButton(
                text=button_text[:64],
                url=button_url,
                style=button_style,
            )
        ])

    if not keyboard:
        return None

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# SEND PAYLOAD
# ============================================================

async def send_payload(
    bot,
    chat_id,
    payload,
):

    keyboard = build_keyboard(payload)

    photo_id = payload.get(
        "photo_file_id"
    )

    text = payload.get(
        "text"
    ) or ""

    if photo_id:

        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=text[:1024] if text else None,
            reply_markup=keyboard,
        )

    if not text:
        text = " "

    return await bot.send_message(
        chat_id=chat_id,
        text=text[:4096],
        reply_markup=keyboard,
    )


# ============================================================
# REGISTER CHAT
# ============================================================

async def register_chat(chat):

    if not chat:
        return

    if chat.type not in (
        "group",
        "supergroup",
        "channel",
    ):
        return

    try:

        await chats_col.update_one(
            {
                "chat_id": chat.id
            },
            {
                "$set": {
                    "chat_id": chat.id,
                    "title": (
                        chat.title
                        or str(chat.id)
                    ),
                    "type": chat.type,
                    "enabled": True,
                    "updated_at": now(),
                },
                "$setOnInsert": {
                    "created_at": now(),
                },
            },
            upsert=True,
        )

    except Exception as e:

        log.error(
            "Register chat error: %s",
            e,
        )


# ============================================================
# BOT ADDED / REMOVED
# ============================================================

async def my_chat_member_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if not chat:
        return

    member_update = update.my_chat_member

    if not member_update:
        return

    new_status = (
        member_update.new_chat_member.status
    )

    log.info(
        "MY CHAT MEMBER UPDATE | chat=%s | status=%s",
        chat.id,
        new_status,
    )

    if new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):

        await register_chat(chat)

        if chat.type in (
            "group",
            "supergroup",
        ):

            try:

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "✅ Promotion system activated.\n\n"
                        "This chat has been registered automatically."
                    ),
                )

            except Exception as e:

                log.info(
                    "Activation message skipped: %s",
                    e,
                )

    elif new_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.BANNED,
        "kicked",
    ):

        try:

            await chats_col.update_one(
                {
                    "chat_id": chat.id
                },
                {
                    "$set": {
                        "enabled": False,
                        "updated_at": now(),
                    }
                },
            )

        except Exception as e:

            log.error(
                "Disable chat error: %s",
                e,
            )


# ============================================================
# CHAT MEMBER
# ============================================================

async def chat_member_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if not chat:
        return

    await register_chat(chat)


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    existing = await users_col.find_one(
        {
            "user_id": user.id
        }
    )

    if existing:

        await users_col.update_one(
            {
                "user_id": user.id
            },
            {
                "$set": {
                    "name": user.full_name,
                    "username": user.username,
                    "last_seen_at": now(),
                    "blocked": False,
                }
            },
        )

        return

    await users_col.insert_one(
        {
            "user_id": user.id,
            "name": user.full_name,
            "username": user.username,
            "first_started_at": now(),
            "last_seen_at": now(),
            "blocked": False,
        }
    )

    await update.effective_message.reply_text(
        WELCOME_TEXT
    )


# ============================================================
# USERS
# ============================================================

@admin_only
async def users(
    update,
    context,
):

    total = await users_col.count_documents({})

    active = await users_col.count_documents(
        {
            "blocked": {
                "$ne": True
            }
        }
    )

    blocked = await users_col.count_documents(
        {
            "blocked": True
        }
    )

    await update.effective_message.reply_text(
        "👥 USER STATISTICS\n\n"
        f"📊 Total Users: {total}\n"
        f"🟢 Active: {active}\n"
        f"🔴 Blocked: {blocked}"
    )


# ============================================================
# CHATS
# ============================================================

@admin_only
async def chats(
    update,
    context,
):

    total = await chats_col.count_documents({})

    active = await chats_col.count_documents(
        {
            "enabled": True
        }
    )

    groups = await chats_col.count_documents(
        {
            "enabled": True,
            "type": {
                "$in": [
                    "group",
                    "supergroup",
                ]
            },
        }
    )

    channels = await chats_col.count_documents(
        {
            "enabled": True,
            "type": "channel",
        }
    )

    await update.effective_message.reply_text(
        "📊 CHAT STATISTICS\n\n"
        f"👥 Groups: {groups}\n"
        f"📢 Channels: {channels}\n"
        f"📦 Total Active: {active}\n"
        f"🗃️ Total Records: {total}"
    )


# ============================================================
# HELP - ADMIN ONLY
# ============================================================

@admin_only
async def help_command(
    update,
    context,
):

    await update.effective_message.reply_text(
        "🔥 PROMOTION BOT — ADMIN PANEL\n\n"

        "👥 USERS\n"
        "/users\n"
        "/ubroadcast\n\n"

        "📢 BROADCAST\n"
        "/broadcast\n\n"

        "🔄 SCHEDULE\n"
        "/schedule\n"
        "/schedules\n"
        "/pause ID\n"
        "/resume ID\n"
        "/stopall\n"
        "/delete_schedule ID\n"
        "/deleteall\n\n"

        "🎯 PROMOTION SETS\n"
        "/sets\n"
        "/setstatus\n"
        "/clearsets\n\n"

        "📊 CHATS\n"
        "/chats\n\n"

        "❌ /cancel\n\n"

        "━━━━━━━━━━━━━━\n"
        "🎨 BUTTON COLOURS\n"
        "━━━━━━━━━━━━━━\n\n"

        "primary = 🔵 Blue\n"
        "success = 🟢 Green\n"
        "danger = 🔴 Red\n\n"

        "Maximum 3 buttons per set."
    )


# ============================================================
# SET SYSTEM
#
# 15 LINKS
# ↓
# 5 SETS
# ↓
# 3 BUTTONS PER SET
# ============================================================

async def get_promotion_sets():

    doc = await sets_col.find_one(
        {
            "_id": "main"
        }
    )

    if not doc:
        return []

    return doc.get(
        "sets",
        []
    )


# ============================================================
# /sets
# ============================================================

@admin_only
async def promotion_sets(
    update,
    context,
):

    context.user_data["set_setup"] = {
        "step": "waiting_lines"
    }

    await update.effective_message.reply_text(
        "🎯 PROMOTION SETUP\n\n"

        "15 പേരും 15 ലിങ്കും ഇവിടെ അയക്കുക.\n\n"

        "ഓരോ line-ലും:\n"
        "പേര് | ലിങ്ക്\n\n"

        "Example:\n\n"

        "Person 1 | https://t.me/link1\n"
        "Person 2 | https://t.me/link2\n"
        "Person 3 | https://t.me/link3\n"
        "Person 4 | https://t.me/link4\n"
        "...\n"
        "Person 15 | https://t.me/link15\n\n"

        "🎨 Color വേണമെങ്കിൽ:\n"
        "പേര് | ലിങ്ക് | primary\n\n"

        "Colors:\n"
        "🔵 primary\n"
        "🟢 success\n"
        "🔴 danger\n\n"

        "⚠️ Exactly 15 lines വേണം.\n\n"
        "/cancel — cancel"
    )


# ============================================================
# SAVE 15 LINKS
# ============================================================

async def capture_promotion_sets(
    update,
    context,
):

    state = context.user_data.get(
        "set_setup"
    )

    if not state:
        return

    if state.get("step") != "waiting_lines":
        return

    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()

    if text.lower() == "/cancel":

        context.user_data.pop(
            "set_setup",
            None,
        )

        return

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    # --------------------------------------------------------
    # EXACTLY 15
    # --------------------------------------------------------

    if len(lines) != 15:

        await message.reply_text(
            "❌ Exactly 15 lines വേണം.\n\n"
            f"നിങ്ങൾ അയച്ചത്: {len(lines)}\n\n"
            "Example:\n"
            "Name 1 | https://t.me/link1\n"
            "Name 2 | https://t.me/link2\n"
            "...\n"
            "Name 15 | https://t.me/link15"
        )

        return

    buttons = []

    for index, line in enumerate(
        lines,
        start=1,
    ):

        parts = [
            x.strip()
            for x in line.split("|")
        ]

        if len(parts) == 2:

            button_text = parts[0]
            button_url = parts[1]
            style = "primary"

        elif len(parts) == 3:

            button_text = parts[0]
            button_url = parts[1]
            style = parts[2].lower()

        else:

            await message.reply_text(
                f"❌ Line {index} invalid.\n\n"
                "Format:\n"
                "Name | URL | Color"
            )

            return

        if not button_text:

            await message.reply_text(
                f"❌ Line {index}: Name missing."
            )

            return

        if not (
            button_url.startswith("https://")
            or button_url.startswith("http://")
            or button_url.startswith("tg://")
        ):

            await message.reply_text(
                f"❌ Line {index}: Invalid URL.\n\n"
                f"{button_url}"
            )

            return

        if style not in (
            "primary",
            "success",
            "danger",
        ):

            await message.reply_text(
                f"❌ Line {index}: Invalid colour.\n\n"
                "Use:\n"
                "primary\n"
                "success\n"
                "danger"
            )

            return

        buttons.append(
            {
                "text": button_text[:64],
                "url": button_url,
                "style": style,
            }
        )

    # --------------------------------------------------------
    # AUTOMATIC 5 SETS
    # --------------------------------------------------------

    promotion_sets_data = []

    for i in range(5):

        start = i * 3
        end = start + 3

        promotion_sets_data.append(
            {
                "set_number": i + 1,
                "buttons": buttons[start:end],
            }
        )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    await sets_col.update_one(
        {
            "_id": "main"
        },
        {
            "$set": {
                "sets": promotion_sets_data,
                "total_links": 15,
                "total_sets": 5,
                "updated_at": now(),
            },
            "$setOnInsert": {
                "created_at": now(),
            },
        },
        upsert=True,
    )

    context.user_data.pop(
        "set_setup",
        None,
    )

    await message.reply_text(
        "✅ PROMOTION SETS SAVED\n\n"

        "📦 Total links: 15\n"
        "🎯 Total sets: 5\n"
        "🔘 Buttons per set: 3\n\n"

        "SET 1 → Link 1–3\n"
        "SET 2 → Link 4–6\n"
        "SET 3 → Link 7–9\n"
        "SET 4 → Link 10–12\n"
        "SET 5 → Link 13–15\n\n"

        "🔄 Schedule ഓടുമ്പോൾ sets "
        "മാറിമാറി പോകും.\n\n"

        "📌 /setstatus"
    )


# ============================================================
# SET STATUS
# ============================================================

@admin_only
async def set_status(
    update,
    context,
):

    sets = await get_promotion_sets()

    if not sets:

        await update.effective_message.reply_text(
            "❌ Promotion sets ഇതുവരെ set ചെയ്തിട്ടില്ല.\n\n"
            "Use /sets"
        )

        return

    text = (
        "🎯 PROMOTION SET STATUS\n\n"
        "━━━━━━━━━━━━━━\n"
    )

    for item in sets:

        number = item.get(
            "set_number"
        )

        text += (
            f"📦 SET {number}\n"
        )

        for button in item.get(
            "buttons",
            []
        ):

            text += (
                f"🔘 {button.get('text')}\n"
                f"🔗 {button.get('url')}\n"
                f"🎨 {button.get('style')}\n\n"
            )

        text += "━━━━━━━━━━━━━━\n"

    await update.effective_message.reply_text(
        text[:4096]
    )


# ============================================================
# CLEAR SETS
# ============================================================

@admin_only
async def clear_sets(
    update,
    context,
):

    await sets_col.delete_one(
        {
            "_id": "main"
        }
    )

    await update.effective_message.reply_text(
        "🗑 PROMOTION SETS CLEARED.\n\n"
        "Use /sets to create them again."
    )


# ============================================================
# BEGIN SETUP
# ============================================================

async def begin_setup(
    update,
    context,
    mode,
):

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


# ============================================================
# BROADCAST
# ============================================================

@admin_only
async def broadcast(
    update,
    context,
):

    await begin_setup(
        update,
        context,
        "broadcast",
    )


# ============================================================
# USER BROADCAST
# ============================================================

@admin_only
async def ubroadcast(
    update,
    context,
):

    await begin_setup(
        update,
        context,
        "ubroadcast",
    )


# ============================================================
# SCHEDULE
# ============================================================

@admin_only
async def schedule(
    update,
    context,
):

    await begin_setup(
        update,
        context,
        "schedule",
    )


# ============================================================
# CAPTURE CONTENT
# ============================================================

async def capture_content(
    update,
    context,
):

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    if setup.get("step") != "content":
        return

    message = update.effective_message

    if message.photo:

        setup["photo_file_id"] = (
            message.photo[-1].file_id
        )

        setup["text"] = (
            message.caption or ""
        )

    elif message.text:

        setup["photo_file_id"] = None
        setup["text"] = message.text

    else:

        return

    setup["step"] = "button"

    await message.reply_text(
        "🎨 MULTI BUTTON SETUP\n\n"

        "Maximum 3 buttons.\n\n"

        "Format:\n"
        "Button Text | URL | Color\n\n"

        "Example:\n\n"

        "🎬 Watch | https://example.com | primary\n"
        "👥 Join | https://t.me/group | success\n"
        "📢 Channel | https://t.me/channel | danger\n\n"

        "Or send:\n"
        "skip"
    )


# ============================================================
# CAPTURE BUTTONS
# ============================================================

async def capture_button(
    update,
    context,
):

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    if setup.get("step") != "button":
        return

    text = (
        update.effective_message.text
        or ""
    ).strip()

    if text.lower() == "skip":

        setup["buttons"] = []

    else:

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        if len(lines) > 3:

            await update.effective_message.reply_text(
                "❌ Maximum 3 buttons മാത്രം."
            )

            return

        buttons = []

        for line in lines:

            parts = [
                x.strip()
                for x in line.split("|")
            ]

            if len(parts) == 2:

                button_text = parts[0]
                button_url = parts[1]
                button_style = "primary"

            elif len(parts) == 3:

                button_text = parts[0]
                button_url = parts[1]
                button_style = parts[2].lower()

            else:

                await update.effective_message.reply_text(
                    "❌ Invalid format.\n\n"
                    "Button Text | URL | Color"
                )

                return

            if not button_text:

                await update.effective_message.reply_text(
                    "❌ Button text cannot be empty."
                )

                return

            if not (
                button_url.startswith("https://")
                or button_url.startswith("http://")
                or button_url.startswith("tg://")
            ):

                await update.effective_message.reply_text(
                    f"❌ Invalid URL:\n{button_url}"
                )

                return

            if button_style not in (
                "primary",
                "success",
                "danger",
            ):

                await update.effective_message.reply_text(
                    "❌ Invalid colour.\n\n"
                    "primary\n"
                    "success\n"
                    "danger"
                )

                return

            buttons.append(
                {
                    "text": button_text[:64],
                    "url": button_url,
                    "style": button_style,
                }
            )

        setup["buttons"] = buttons

    if setup["mode"] == "broadcast":

        await execute_chat_broadcast(
            update,
            context,
        )

        return

    if setup["mode"] == "ubroadcast":

        await execute_user_broadcast(
            update,
            context,
        )

        return

    if setup["mode"] == "schedule":

        setup["step"] = "interval"

        await update.effective_message.reply_text(
            "⏱️ SELECT INTERVAL\n\n"

            "5m\n"
            "10m\n"
            "15m\n"
            "30m\n"
            "1h\n\n"

            "Example:\n"
            "5m"
        )


# ============================================================
# SAFE CHAT SEND
# ============================================================

async def safe_chat_send(
    bot,
    chat_id,
    payload,
):

    for attempt in range(
        MAX_RETRIES + 1
    ):

        try:

            return await send_payload(
                bot,
                chat_id,
                payload,
            )

        except RetryAfter as e:

            wait = int(
                getattr(
                    e,
                    "retry_after",
                    1,
                )
            ) + 2

            await asyncio.sleep(
                wait
            )

        except (
            TimedOut,
            NetworkError,
        ):

            if attempt >= MAX_RETRIES:
                raise

            await asyncio.sleep(
                2 ** attempt
            )

    raise RuntimeError(
        "Chat send failed"
    )


# ============================================================
# CHAT BROADCAST
# ============================================================

async def execute_chat_broadcast(
    update,
    context,
):

    setup = context.user_data.get(
        "setup"
    )

    payload = build_payload(
        setup
    )

    rows = await chats_col.find(
        {
            "enabled": True
        }
    ).to_list(
        length=5000
    )

    total = len(rows)

    if total == 0:

        context.user_data.pop(
            "setup",
            None,
        )

        await update.effective_message.reply_text(
            "❌ No registered groups/channels."
        )

        return

    progress = await update.effective_message.reply_text(
        "📢 BROADCAST STARTING...\n\n"
        f"📦 Total: {total}\n"
        "✅ Sent: 0\n"
        f"⏳ Remaining: {total}\n"
        "❌ Failed: 0\n"
        "📊 Progress: 0%"
    )

    sent = 0
    failed = 0

    for index, row in enumerate(
        rows,
        start=1,
    ):

        try:

            await safe_chat_send(
                context.bot,
                row["chat_id"],
                payload,
            )

            sent += 1

        except Exception as e:

            failed += 1

            log.warning(
                "Broadcast failed: %s",
                e,
            )

        await asyncio.sleep(
            CHAT_SEND_DELAY
        )

        if (
            index % PROGRESS_EVERY == 0
            or index == total
        ):

            remaining = total - index

            percent = round(
                index / total * 100,
                1,
            )

            try:

                await progress.edit_text(
                    "📢 BROADCASTING...\n\n"
                    f"📦 Total: {total}\n"
                    f"✅ Sent: {sent}\n"
                    f"⏳ Remaining: {remaining}\n"
                    f"❌ Failed: {failed}\n"
                    f"📊 Progress: {percent}%"
                )

            except Exception:
                pass

    context.user_data.pop(
        "setup",
        None,
    )

    await progress.edit_text(
        "✅ BROADCAST COMPLETED\n\n"
        f"📦 Total: {total}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}"
    )


# ============================================================
# SAFE USER SEND
# ============================================================

async def safe_user_send(
    bot,
    user_id,
    payload,
):

    for attempt in range(
        MAX_RETRIES + 1
    ):

        try:

            return await send_payload(
                bot,
                user_id,
                payload,
            )

        except RetryAfter as e:

            wait = int(
                getattr(
                    e,
                    "retry_after",
                    1,
                )
            ) + 2

            await asyncio.sleep(
                wait
            )

        except (
            TimedOut,
            NetworkError,
        ):

            if attempt >= MAX_RETRIES:
                raise

            await asyncio.sleep(
                2 ** attempt
            )

    raise RuntimeError(
        "Could not send message"
    )


# ============================================================
# USER BROADCAST
# ============================================================

async def execute_user_broadcast(
    update,
    context,
):

    setup = context.user_data.get(
        "setup"
    )

    payload = build_payload(
        setup
    )

    total = await users_col.count_documents(
        {
            "blocked": {
                "$ne": True
            }
        }
    )

    if total == 0:

        context.user_data.pop(
            "setup",
            None,
        )

        await update.effective_message.reply_text(
            "❌ No users."
        )

        return

    progress = await update.effective_message.reply_text(
        "📢 USER BROADCAST STARTING...\n\n"
        f"👥 Total: {total}\n"
        "✅ Sent: 0\n"
        f"⏳ Remaining: {total}\n"
        "❌ Failed: 0\n"
        "📊 Progress: 0%"
    )

    sent = 0
    failed = 0
    processed = 0

    cursor = users_col.find(
        {
            "blocked": {
                "$ne": True
            }
        },
        {
            "user_id": 1
        },
    ).sort(
        "_id",
        1,
    )

    async for row in cursor:

        user_id = int(
            row["user_id"]
        )

        try:

            await safe_user_send(
                context.bot,
                user_id,
                payload,
            )

            sent += 1

        except Exception as e:

            failed += 1

            error = str(e).lower()

            if (
                "blocked" in error
                or "deactivated" in error
                or "chat not found" in error
                or "user is deactivated" in error
            ):

                await users_col.update_one(
                    {
                        "user_id": user_id
                    },
                    {
                        "$set": {
                            "blocked": True,
                            "blocked_at": now(),
                        }
                    },
                )

        processed += 1

        await asyncio.sleep(
            USER_SEND_DELAY
        )

        if (
            processed % PROGRESS_EVERY == 0
            or processed == total
        ):

            remaining = max(
                total - processed,
                0,
            )

            percent = round(
                processed / total * 100,
                1,
            )

            try:

                await progress.edit_text(
                    "📢 USER BROADCASTING...\n\n"
                    f"👥 Total: {total}\n"
                    f"✅ Sent: {sent}\n"
                    f"⏳ Remaining: {remaining}\n"
                    f"❌ Failed: {failed}\n"
                    f"📊 Progress: {percent}%"
                )

            except Exception:
                pass

    context.user_data.pop(
        "setup",
        None,
    )

    await progress.edit_text(
        "✅ USER BROADCAST COMPLETED\n\n"
        f"👥 Total: {total}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}"
    )


# ============================================================
# INTERVALS
# ============================================================

INTERVALS = {
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}


# ============================================================
# CREATE SCHEDULE
# ============================================================

async def capture_interval(
    update,
    context,
):

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    if setup.get("step") != "interval":
        return

    value = (
        update.effective_message.text
        or ""
    ).strip().lower()

    if value not in INTERVALS:

        await update.effective_message.reply_text(
            "❌ Invalid interval.\n\n"
            "5m\n"
            "10m\n"
            "15m\n"
            "30m\n"
            "1h"
        )

        return

    payload = build_payload(
        setup
    )

    # --------------------------------------------------------
    # Each schedule has its own random order.
    # --------------------------------------------------------

    sets = await get_promotion_sets()

    set_order = list(range(5))

    if len(sets) == 5:
        random.shuffle(set_order)

    doc = {
        "interval": INTERVALS[value],
        "interval_label": value,
        "payload": payload,
        "enabled": True,
        "last_messages": {},
        "set_order": set_order,
        "set_position": 0,
        "created_at": now(),
        "updated_at": now(),
    }

    result = await schedules_col.insert_one(
        doc
    )

    schedule_id = str(
        result.inserted_id
    )

    await run_schedule(
        schedule_id,
        context.application,
    )

    context.application.job_queue.run_repeating(
        scheduled_job,
        interval=INTERVALS[value],
        first=INTERVALS[value],
        data={
            "schedule_id": schedule_id
        },
        name=f"schedule:{schedule_id}",
    )

    context.user_data.pop(
        "setup",
        None,
    )

    await update.effective_message.reply_text(
        "✅ SCHEDULE CREATED\n\n"
        f"🆔 {schedule_id}\n"
        f"⏱ Every {value}\n\n"
        "🎯 5 promotion sets enabled.\n"
        "🔄 Sets will rotate automatically.\n"
        "🎲 Random order is used.\n\n"
        "🗑 Previous scheduled message "
        "will be deleted before the next one.\n\n"
        f"/delete_schedule {schedule_id}"
    )


# ============================================================
# DELETE MESSAGE
# ============================================================

async def delete_message_safe(
    bot,
    chat_id,
    message_id,
):

    if not message_id:
        return False

    for attempt in range(
        MAX_RETRIES + 1
    ):

        try:

            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

            return True

        except RetryAfter as e:

            wait = int(
                getattr(
                    e,
                    "retry_after",
                    1,
                )
            ) + 2

            await asyncio.sleep(
                wait
            )

        except (
            TimedOut,
            NetworkError,
        ):

            if attempt >= MAX_RETRIES:
                return False

            await asyncio.sleep(
                2 ** attempt
            )

        except Exception:

            return False

    return False


# ============================================================
# GET NEXT PROMOTION SET
# ============================================================

async def get_next_set(
    schedule_doc,
):

    sets = await get_promotion_sets()

    if len(sets) != 5:
        return None

    order = schedule_doc.get(
        "set_order",
        []
    )

    position = int(
        schedule_doc.get(
            "set_position",
            0
        )
    )

    if len(order) != 5:

        order = list(range(5))
        random.shuffle(order)

    index = order[position % 5]

    return sets[index]


# ============================================================
# RUN SCHEDULE
# ============================================================

async def run_schedule(
    schedule_id,
    application,
):

    try:

        object_id = ObjectId(
            schedule_id
        )

    except Exception:

        return

    doc = await schedules_col.find_one(
        {
            "_id": object_id,
            "enabled": True,
        }
    )

    if not doc:
        return

    rows = await chats_col.find(
        {
            "enabled": True
        }
    ).to_list(
        length=5000
    )

    if not rows:
        return

    old_messages = doc.get(
        "last_messages",
        {}
    )

    new_messages = {}

    # ========================================================
    # PROMOTION SET MODE
    # ========================================================

    sets = await get_promotion_sets()

    use_sets = len(sets) == 5

    current_position = int(
        doc.get(
            "set_position",
            0
        )
    )

    order = doc.get(
        "set_order",
        []
    )

    if use_sets:

        if len(order) != 5:

            order = list(range(5))
            random.shuffle(order)

        current_set_index = order[
            current_position % 5
        ]

    # ========================================================
    # SEND TO EVERY CHAT
    # ========================================================

    for row in rows:

        chat_id = row["chat_id"]
        key = str(chat_id)

        # ----------------------------------------------------
        # DELETE OLD MESSAGE
        # ----------------------------------------------------

        old_message_id = old_messages.get(
            key
        )

        if old_message_id:

            await delete_message_safe(
                application.bot,
                chat_id,
                old_message_id,
            )

        # ----------------------------------------------------
        # SELECT PROMOTION
        # ----------------------------------------------------

        if use_sets:

            selected_set = sets[
                current_set_index
            ]

            payload = {
                "photo_file_id": doc.get(
                    "payload",
                    {}
                ).get(
                    "photo_file_id"
                ),
                "text": doc.get(
                    "payload",
                    {}
                ).get(
                    "text",
                    ""
                ),
                "buttons": selected_set.get(
                    "buttons",
                    []
                ),
            }

        else:

            # Fallback to normal schedule
            payload = doc.get(
                "payload",
                {}
            )

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        try:

            message = await safe_chat_send(
                application.bot,
                chat_id,
                payload,
            )

            new_messages[key] = (
                message.message_id
            )

        except Exception as e:

            log.warning(
                "Schedule failed chat=%s error=%s",
                chat_id,
                e,
            )

        await asyncio.sleep(
            CHAT_SEND_DELAY
        )

    # ========================================================
    # NEXT SET
    # ========================================================

    update_data = {
        "last_messages": new_messages,
        "updated_at": now(),
    }

    if use_sets:

        next_position = (
            current_position + 1
        )

        # ----------------------------------------------------
        # After all 5 sets:
        # create a new random order.
        # ----------------------------------------------------

        if next_position >= 5:

            new_order = list(
                range(5)
            )

            random.shuffle(
                new_order
            )

            # Never start next cycle with
            # the same set that ended previous cycle.
            if (
                new_order[0]
                == order[4]
                and len(new_order) > 1
            ):

                new_order[0], new_order[1] = (
                    new_order[1],
                    new_order[0],
                )

            update_data[
                "set_order"
            ] = new_order

            update_data[
                "set_position"
            ] = 0

        else:

            update_data[
                "set_position"
            ] = next_position

    await schedules_col.update_one(
        {
            "_id": object_id
        },
        {
            "$set": update_data
        },
    )


# ============================================================
# SCHEDULE JOB
# ============================================================

async def scheduled_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    job_data = (
        context.job.data or {}
    )

    schedule_id = job_data.get(
        "schedule_id"
    )

    if not schedule_id:
        return

    log.info(
        "RUNNING SCHEDULE: %s",
        schedule_id,
    )

    await run_schedule(
        schedule_id,
        context.application,
    )


# ============================================================
# RESTORE SCHEDULES
# ============================================================

async def restore_schedules(
    application,
):

    rows = await schedules_col.find(
        {
            "enabled": True
        }
    ).to_list(
        length=1000
    )

    restored = 0

    for row in rows:

        schedule_id = str(
            row["_id"]
        )

        for job in (
            application
            .job_queue
            .get_jobs_by_name(
                f"schedule:{schedule_id}"
            )
        ):

            job.schedule_removal()

        application.job_queue.run_repeating(
            scheduled_job,
            interval=int(
                row["interval"]
            ),
            first=int(
                row["interval"]
            ),
            data={
                "schedule_id": schedule_id
            },
            name=f"schedule:{schedule_id}",
        )

        restored += 1

    log.info(
        "Restored %d schedules",
        restored,
    )


# ============================================================
# SCHEDULE LIST
# ============================================================

@admin_only
async def schedules(
    update,
    context,
):

    rows = await schedules_col.find().sort(
        "created_at",
        -1,
    ).to_list(
        length=100
    )

    if not rows:

        await update.effective_message.reply_text(
            "❌ No schedules."
        )

        return

    lines = [
        "🔄 SCHEDULES\n"
    ]

    for row in rows:

        sid = str(
            row["_id"]
        )

        status = (
            "🟢 ON"
            if row.get("enabled")
            else "⏸ OFF"
        )

        position = int(
            row.get(
                "set_position",
                0
            )
        )

        lines.append(
            f"{status}\n"
            f"🆔 {sid}\n"
            f"⏱ Every {row.get('interval_label', '-')}\n"
            f"🎯 Next Set: {(position % 5) + 1}\n"
        )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# PAUSE
# ============================================================

@admin_only
async def pause(
    update,
    context,
):

    if not context.args:

        await update.effective_message.reply_text(
            "/pause <schedule_id>"
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

    result = await schedules_col.update_one(
        {
            "_id": object_id
        },
        {
            "$set": {
                "enabled": False,
                "updated_at": now(),
            }
        },
    )

    for job in (
        context.application
        .job_queue
        .get_jobs_by_name(
            f"schedule:{sid}"
        )
    ):

        job.schedule_removal()

    if result.matched_count:

        await update.effective_message.reply_text(
            "⏸ Schedule paused."
        )

    else:

        await update.effective_message.reply_text(
            "❌ Schedule not found."
        )


# ============================================================
# RESUME
# ============================================================

@admin_only
async def resume(
    update,
    context,
):

    if not context.args:

        await update.effective_message.reply_text(
            "/resume <schedule_id>"
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

    row = await schedules_col.find_one(
        {
            "_id": object_id
        }
    )

    if not row:

        await update.effective_message.reply_text(
            "❌ Schedule not found."
        )

        return

    await schedules_col.update_one(
        {
            "_id": object_id
        },
        {
            "$set": {
                "enabled": True,
                "updated_at": now(),
            }
        },
    )

    for job in (
        context.application
        .job_queue
        .get_jobs_by_name(
            f"schedule:{sid}"
        )
    ):

        job.schedule_removal()

    context.application.job_queue.run_repeating(
        scheduled_job,
        interval=int(
            row["interval"]
        ),
        first=1,
        data={
            "schedule_id": sid
        },
        name=f"schedule:{sid}",
    )

    await update.effective_message.reply_text(
        "▶️ Schedule resumed."
    )


# ============================================================
# STOP ALL
# ============================================================

@admin_only
async def stop_all_schedules(
    update,
    context,
):

    result = await schedules_col.update_many(
        {
            "enabled": True
        },
        {
            "$set": {
                "enabled": False,
                "updated_at": now(),
            }
        },
    )

    removed_jobs = 0

    for job in (
        context.application
        .job_queue
        .jobs()
    ):

        if (
            job.name
            and job.name.startswith(
                "schedule:"
            )
        ):

            job.schedule_removal()

            removed_jobs += 1

    await update.effective_message.reply_text(
        "🛑 ALL SCHEDULES STOPPED\n\n"
        f"🔴 Stopped: {result.modified_count}\n"
        f"⏹ Jobs removed: {removed_jobs}\n\n"
        "💾 Schedules are still saved."
    )


# ============================================================
# DELETE ONE SCHEDULE
# ============================================================

@admin_only
async def delete_schedule(
    update,
    context,
):

    if not context.args:

        await update.effective_message.reply_text(
            "/delete_schedule <schedule_id>"
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

    row = await schedules_col.find_one(
        {
            "_id": object_id
        }
    )

    if not row:

        await update.effective_message.reply_text(
            "❌ Schedule not found."
        )

        return

    deleted = 0
    failed = 0

    for chat_id, message_id in row.get(
        "last_messages",
        {}
    ).items():

        result = await delete_message_safe(
            context.bot,
            int(chat_id),
            int(message_id),
        )

        if result:
            deleted += 1
        else:
            failed += 1

    for job in (
        context.application
        .job_queue
        .get_jobs_by_name(
            f"schedule:{sid}"
        )
    ):

        job.schedule_removal()

    await schedules_col.delete_one(
        {
            "_id": object_id
        }
    )

    await update.effective_message.reply_text(
        "🗑 SCHEDULE DELETED\n\n"
        f"🆔 {sid}\n"
        f"✅ Messages deleted: {deleted}\n"
        f"❌ Failed: {failed}"
    )


# ============================================================
# DELETE ALL SCHEDULE MESSAGES
# ============================================================

@admin_only
async def delete_all_schedule_messages(
    update,
    context,
):

    rows = await schedules_col.find(
        {}
    ).to_list(
        length=1000
    )

    if not rows:

        await update.effective_message.reply_text(
            "ℹ️ No schedules found."
        )

        return

    total_messages = 0
    deleted = 0
    failed = 0

    for row in rows:

        last_messages = row.get(
            "last_messages",
            {}
        )

        total_messages += len(
            last_messages
        )

        for chat_id, message_id in (
            last_messages.items()
        ):

            result = await delete_message_safe(
                context.bot,
                int(chat_id),
                int(message_id),
            )

            if result:
                deleted += 1
            else:
                failed += 1

        await schedules_col.update_one(
            {
                "_id": row["_id"]
            },
            {
                "$set": {
                    "last_messages": {},
                    "updated_at": now(),
                }
            },
        )

    await update.effective_message.reply_text(
        "🧹 ALL SCHEDULE MESSAGES PROCESSED\n\n"
        f"📦 Tracked: {total_messages}\n"
        f"✅ Deleted: {deleted}\n"
        f"❌ Failed: {failed}\n\n"
        "⚠️ Active schedules will send again "
        "at the next interval."
    )


# ============================================================
# CANCEL
# ============================================================

@admin_only
async def cancel(
    update,
    context,
):

    context.user_data.pop(
        "setup",
        None,
    )

    context.user_data.pop(
        "set_setup",
        None,
    )

    await update.effective_message.reply_text(
        "❌ Cancelled."
    )


# ============================================================
# SETUP ROUTER
# ============================================================

async def setup_router(
    update,
    context,
):

    # --------------------------------------------------------
    # PROMOTION SETUP HAS PRIORITY
    # --------------------------------------------------------

    if context.user_data.get(
        "set_setup"
    ):

        await capture_promotion_sets(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # NORMAL SETUP
    # --------------------------------------------------------

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    step = setup.get(
        "step"
    )

    if step == "content":

        await capture_content(
            update,
            context,
        )

    elif step == "button":

        await capture_button(
            update,
            context,
        )

    elif step == "interval":

        await capture_interval(
            update,
            context,
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context,
):

    log.error(
        "Unhandled error: %s",
        context.error,
        exc_info=True,
    )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application,
):

    global mongo
    global db
    global users_col
    global chats_col
    global schedules_col
    global sets_col

    if not MONGO_URI:

        raise RuntimeError(
            "MONGO_URI is missing."
        )

    mongo = AsyncIOMotorClient(
        MONGO_URI
    )

    db = mongo[DB_NAME]

    users_col = db["users"]
    chats_col = db["chats"]
    schedules_col = db["schedules"]
    sets_col = db["promotion_sets"]

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
        "_id",
        unique=True,
    )

    await restore_schedules(
        application
    )

    log.info(
        "Database initialized."
    )


# ============================================================
# POST SHUTDOWN
# ============================================================

async def post_shutdown(
    application,
):

    global mongo

    if mongo:

        mongo.close()

        log.info(
            "MongoDB connection closed."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing."
        )

    if not ADMIN_IDS:

        raise RuntimeError(
            "ADMIN_IDS is missing."
        )

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
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "users",
            users,
        )
    )

    application.add_handler(
        CommandHandler(
            "chats",
            chats,
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast",
            broadcast,
        )
    )

    application.add_handler(
        CommandHandler(
            "ubroadcast",
            ubroadcast,
        )
    )

    application.add_handler(
        CommandHandler(
            "schedule",
            schedule,
        )
    )

    application.add_handler(
        CommandHandler(
            "schedules",
            schedules,
        )
    )

    application.add_handler(
        CommandHandler(
            "pause",
            pause,
        )
    )

    application.add_handler(
        CommandHandler(
            "resume",
            resume,
        )
    )

    application.add_handler(
        CommandHandler(
            "stopall",
            stop_all_schedules,
        )
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
        CommandHandler(
            "sets",
            promotion_sets,
        )
    )

    application.add_handler(
        CommandHandler(
            "setstatus",
            set_status,
        )
    )

    application.add_handler(
        CommandHandler(
            "clearsets",
            clear_sets,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel,
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
    # TEXT / PHOTO SETUP
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.PHOTO
            | (
                filters.TEXT
                & ~filters.COMMAND
            ),
            setup_router,
        )
    )

    # ========================================================
    # ERROR
    # ========================================================

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


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
