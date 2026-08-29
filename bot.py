import os
import asyncio
import logging
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
# RATE LIMITS
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

        # Compatibility with old single-button setup
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
# BUILD COLORED MULTI BUTTON KEYBOARD
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

        # ----------------------------------------------------
        # Allowed Telegram styles
        # ----------------------------------------------------

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

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if photo_id:

        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=text[:1024] if text else None,
            reply_markup=keyboard,
        )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

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

        log.info(
            "CHAT REGISTERED: %s | %s | %s",
            chat.title,
            chat.type,
            chat.id,
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

    # --------------------------------------------------------
    # BOT ACTIVE
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # BOT LEFT / KICKED
    # --------------------------------------------------------

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

        log.info(
            "BOT REMOVED: %s",
            chat.id,
        )


# ============================================================
# CHAT MEMBER UPDATE
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
# HELP
# ============================================================

@admin_only
async def help_command(
    update,
    context,
):

    await update.effective_message.reply_text(
        "🔥 PROMOTION BOT\n\n"

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

        "📊 CHATS\n"
        "/chats\n\n"

        "❌ /cancel\n\n"

        "🎨 BUTTON FORMAT\n"
        "Button | URL | color\n\n"

        "Available colors:\n"
        "🔵 primary\n"
        "🟢 success\n"
        "🔴 danger\n\n"

        "Maximum 3 buttons."
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

        "Then 3-button setup will appear.\n\n"

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

        "🎬 Watch Video | https://example.com | primary\n"
        "👥 Join Group | https://t.me/group | success\n"
        "📢 Join Channel | https://t.me/channel | danger\n\n"

        "Colors:\n"
        "🔵 primary = Blue\n"
        "🟢 success = Green\n"
        "🔴 danger = Red\n\n"

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

    # --------------------------------------------------------
    # NO BUTTONS
    # --------------------------------------------------------

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

            # ------------------------------------------------
            # Text | URL
            # ------------------------------------------------

            if len(parts) == 2:

                button_text = parts[0]
                button_url = parts[1]
                button_style = "primary"

            # ------------------------------------------------
            # Text | URL | Color
            # ------------------------------------------------

            elif len(parts) == 3:

                button_text = parts[0]
                button_url = parts[1]
                button_style = (
                    parts[2].lower()
                )

            else:

                await update.effective_message.reply_text(
                    "❌ Invalid format.\n\n"
                    "Use:\n"
                    "Button Text | URL | Color"
                )

                return

            # ------------------------------------------------
            # BUTTON TEXT
            # ------------------------------------------------

            if not button_text:

                await update.effective_message.reply_text(
                    "❌ Button text cannot be empty."
                )

                return

            # ------------------------------------------------
            # URL
            # ------------------------------------------------

            if not (
                button_url.startswith(
                    "https://"
                )
                or button_url.startswith(
                    "http://"
                )
                or button_url.startswith(
                    "tg://"
                )
            ):

                await update.effective_message.reply_text(
                    f"❌ Invalid URL:\n{button_url}"
                )

                return

            # ------------------------------------------------
            # COLOR
            # ------------------------------------------------

            if button_style not in (
                "primary",
                "success",
                "danger",
            ):

                await update.effective_message.reply_text(
                    "❌ Invalid color.\n\n"
                    "Use only:\n"
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

    # ========================================================
    # NORMAL BROADCAST
    # ========================================================

    if setup["mode"] == "broadcast":

        await execute_chat_broadcast(
            update,
            context,
        )

        return

    # ========================================================
    # USER BROADCAST
    # ========================================================

    if setup["mode"] == "ubroadcast":

        await execute_user_broadcast(
            update,
            context,
        )

        return

    # ========================================================
    # SCHEDULE
    # ========================================================

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
            "10m"
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

            log.warning(
                "RetryAfter chat=%s wait=%ss",
                chat_id,
                wait,
            )

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
            "❌ No registered groups/channels.\n\n"
            "Add the bot to a group/channel first."
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

        chat_id = row["chat_id"]

        try:

            await safe_chat_send(
                context.bot,
                chat_id,
                payload,
            )

            sent += 1

        except Exception as e:

            failed += 1

            log.warning(
                "Broadcast failed chat=%s error=%s",
                chat_id,
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
            "Choose:\n"
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

    doc = {
        "interval": INTERVALS[value],
        "interval_label": value,
        "payload": payload,
        "enabled": True,
        "last_messages": {},
        "created_at": now(),
        "updated_at": now(),
    }

    result = await schedules_col.insert_one(
        doc
    )

    schedule_id = str(
        result.inserted_id
    )

    # --------------------------------------------------------
    # FIRST SEND
    # --------------------------------------------------------

    await run_schedule(
        schedule_id,
        context.application,
    )

    # --------------------------------------------------------
    # REPEATING JOB
    # --------------------------------------------------------

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

        "🔄 Previous scheduled message "
        "will be deleted.\n"

        "🆕 New promotion will be sent.\n\n"

        "📢 Works automatically for all "
        "registered groups/channels.\n\n"

        "🗑 To delete this schedule:\n"
        f"/delete_schedule {schedule_id}"
    )


# ============================================================
# DELETE MESSAGE SAFE
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

        log.error(
            "Invalid schedule ID: %s",
            schedule_id,
        )

        return

    doc = await schedules_col.find_one(
        {
            "_id": object_id,
            "enabled": True,
        }
    )

    if not doc:
        return

    payload = doc.get(
        "payload",
        {}
    )

    rows = await chats_col.find(
        {
            "enabled": True
        }
    ).to_list(
        length=5000
    )

    old_messages = doc.get(
        "last_messages",
        {}
    )

    new_messages = {}

    for row in rows:

        chat_id = row["chat_id"]

        key = str(chat_id)

        # ----------------------------------------------------
        # DELETE PREVIOUS MESSAGE
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
        # SEND NEW MESSAGE
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
                "Schedule failed %s: %s",
                chat_id,
                e,
            )

        await asyncio.sleep(
            CHAT_SEND_DELAY
        )

    await schedules_col.update_one(
        {
            "_id": object_id
        },
        {
            "$set": {
                "last_messages": new_messages,
                "updated_at": now(),
            }
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

        lines.append(
            f"{status}\n"
            f"🆔 {sid}\n"
            f"⏱ Every {row.get('interval_label', '-')}\n"
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

        object_id = ObjectId(
            sid
        )

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

        object_id = ObjectId(
            sid
        )

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
# STOP ALL SCHEDULES
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

    if (
        result.modified_count == 0
        and removed_jobs == 0
    ):

        await update.effective_message.reply_text(
            "ℹ️ NO ACTIVE SCHEDULES\n\n"
            "There are no running schedules."
        )

        return

    await update.effective_message.reply_text(
        "🛑 ALL SCHEDULES STOPPED\n\n"
        f"🔴 Stopped: {result.modified_count}\n"
        f"⏹️ Jobs removed: {removed_jobs}\n\n"
        "💾 Schedules are still saved.\n"
        "▶️ Use /resume ID to start one again."
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

        object_id = ObjectId(
            sid
        )

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

    # --------------------------------------------------------
    # DELETE ALL CURRENT MESSAGES OF THIS SCHEDULE
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # REMOVE JOB
    # --------------------------------------------------------

    for job in (
        context.application
        .job_queue
        .get_jobs_by_name(
            f"schedule:{sid}"
        )
    ):

        job.schedule_removal()

    # --------------------------------------------------------
    # DELETE DATABASE RECORD
    # --------------------------------------------------------

    await schedules_col.delete_one(
        {
            "_id": object_id
        }
    )

    await update.effective_message.reply_text(
        "🗑 SCHEDULE DELETED\n\n"
        f"🆔 {sid}\n"
        f"✅ Messages deleted: {deleted}\n"
        f"❌ Failed: {failed}\n\n"
        "💾 Schedule record removed."
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

    # --------------------------------------------------------
    # DELETE CURRENT MESSAGE OF EVERY SCHEDULE
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # CLEAR SAVED MESSAGE IDS
        # ----------------------------------------------------

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
        f"📦 Tracked messages: {total_messages}\n"
        f"✅ Deleted: {deleted}\n"
        f"❌ Failed: {failed}\n\n"
        "💾 Schedule settings are still saved.\n\n"
        "⚠️ Active schedules will send a new "
        "message again at their next interval."
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
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    error = context.error

    log.error(
        "Unhandled error: %s",
        error,
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

    # --------------------------------------------------------
    # INDEXES
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # RESTORE SCHEDULES
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # DELETE ALL CURRENT SCHEDULE MESSAGES
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "deleteall",
            delete_all_schedule_messages,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        )
    )

    # ========================================================
    # AUTOMATIC CHAT REGISTRATION
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
    # SETUP CONTENT
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
    # ERROR HANDLER
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    log.info(
        "Starting promotion bot..."
    )

    # ========================================================
    # ONE POLLING INSTANCE
    # ========================================================

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
