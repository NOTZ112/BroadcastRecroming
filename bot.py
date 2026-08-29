import os
import asyncio
import logging
from datetime import datetime, timezone
from functools import wraps

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "broadcast_scheduler")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

# User broadcast safety
USER_SEND_DELAY = float(os.getenv("USER_SEND_DELAY", "0.8"))
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "20"))
MAX_SEND_RETRIES = 3

# One-time welcome message
WELCOME_TEXT = (
    "👋 Welcome!\n\n"
    "🎬 പുതിയ വീഡിയോകൾ\n"
    "👥 നല്ല ഗ്രൂപ്പുകൾ\n"
    "📢 പുതിയ ചാനലുകൾ\n\n"
    "എല്ലാം ഇവിടെ ലഭിക്കും. ❤️"
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("broadcast_scheduler")

# ============================================================
# DATABASE
# ============================================================

mongo = None
db = None
users_col = None
chats_col = None
schedules_col = None


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
# GENERAL HELPERS
# ============================================================

def reset_setup(context):
    context.user_data.pop("setup", None)


async def get_registered_chats():
    return await chats_col.find(
        {"enabled": True}
    ).to_list(length=1000)


# ============================================================
# BUTTON + MESSAGE SENDING
# ============================================================

async def send_payload(bot, chat_id, payload):

    keyboard = None

    button_text = payload.get("button_text")
    button_url = payload.get("button_url")

    if button_text and button_url:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        button_text,
                        url=button_url
                    )
                ]
            ]
        )

    photo_file_id = payload.get("photo_file_id")
    text = payload.get("text") or ""

    # PHOTO
    if photo_file_id:

        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_id,
            caption=text[:1024] if text else None,
            reply_markup=keyboard
        )

    # TEXT
    else:

        return await bot.send_message(
            chat_id=chat_id,
            text=text[:4096],
            reply_markup=keyboard
        )


# ============================================================
# SAFE USER SEND
# ============================================================

async def safe_send_user(bot, user_id, payload):

    last_error = None

    for attempt in range(MAX_SEND_RETRIES + 1):

        try:

            message = await send_payload(
                bot,
                user_id,
                payload
            )

            return message

        except RetryAfter as e:

            last_error = e

            wait_seconds = int(
                getattr(e, "retry_after", 1)
            ) + 2

            log.warning(
                "Telegram rate limit. Waiting %s seconds",
                wait_seconds
            )

            await asyncio.sleep(wait_seconds)

        except (TimedOut, NetworkError) as e:

            last_error = e

            if attempt >= MAX_SEND_RETRIES:
                break

            await asyncio.sleep(
                2 ** attempt
            )

        except Exception as e:

            last_error = e

            # Permanent error.
            break

    if last_error:
        raise last_error

    raise RuntimeError(
        "Unknown Telegram send error"
    )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    existing = await users_col.find_one(
        {
            "user_id": user.id
        }
    )

    # ========================================================
    # EXISTING USER
    # ========================================================

    if existing:

        await users_col.update_one(
            {
                "user_id": user.id
            },
            {
                "$set": {
                    "name": user.full_name,
                    "username": user.username,
                    "last_seen_at": datetime.now(
                        timezone.utc
                    ),
                    "blocked": False
                }
            }
        )

        # IMPORTANT:
        # Do NOT send welcome again.
        return

    # ========================================================
    # FIRST START
    # ========================================================

    await users_col.insert_one(
        {
            "user_id": user.id,
            "name": user.full_name,
            "username": user.username,
            "first_started_at": datetime.now(
                timezone.utc
            ),
            "last_seen_at": datetime.now(
                timezone.utc
            ),
            "welcome_sent": True,
            "blocked": False
        }
    )

    await update.effective_message.reply_text(
        WELCOME_TEXT
    )


# ============================================================
# HELP
# ============================================================

@admin_only
async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.effective_message.reply_text(

        "🔥 Broadcast + Auto Schedule Bot\n\n"

        "👤 USER SYSTEM\n"
        "/users - User statistics\n"
        "/ubroadcast - Broadcast to users\n\n"

        "📢 GROUP / CHANNEL\n"
        "/broadcast - Broadcast to groups/channels\n"
        "/addchat <chat_id> - Add chat\n"
        "/removechat <chat_id> - Remove chat\n"
        "/chats - Registered chats\n\n"

        "🔄 SCHEDULE\n"
        "/schedule - Create recurring schedule\n"
        "/schedules - View schedules\n"
        "/pause <id> - Pause\n"
        "/resume <id> - Resume\n"
        "/delete_schedule <id> - Delete\n\n"

        "/cancel - Cancel current setup"
    )


# ============================================================
# USERS
# ============================================================

@admin_only
async def users(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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

        "👥 USER MANAGEMENT\n\n"

        f"📊 Total Users: {total}\n"
        f"🟢 Active Users: {active}\n"
        f"🔴 Blocked Users: {blocked}"

    )


# ============================================================
# CHAT MANAGEMENT
# ============================================================

@admin_only
async def addchat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.effective_message.reply_text(
            "Usage:\n/addchat -1001234567890"
        )

        return

    try:

        chat_id = int(
            context.args[0]
        )

    except ValueError:

        await update.effective_message.reply_text(
            "❌ Invalid chat ID."
        )

        return

    try:

        chat = await context.bot.get_chat(
            chat_id
        )

        await chats_col.update_one(

            {
                "chat_id": chat_id
            },

            {
                "$set": {
                    "chat_id": chat_id,
                    "title": chat.title or str(chat_id),
                    "type": chat.type,
                    "enabled": True,
                    "updated_at": datetime.now(
                        timezone.utc
                    )
                }
            },

            upsert=True
        )

        await update.effective_message.reply_text(

            "✅ Chat added\n\n"

            f"Title: {chat.title or '-'}\n"
            f"Type: {chat.type}\n"
            f"ID: {chat_id}"

        )

    except Exception as e:

        await update.effective_message.reply_text(

            "❌ Could not add chat.\n\n"
            f"{e}\n\n"
            "Make sure the bot is inside the group/channel "
            "and has permission to post."

        )


@admin_only
async def removechat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.effective_message.reply_text(
            "/removechat -1001234567890"
        )

        return

    try:

        chat_id = int(
            context.args[0]
        )

    except ValueError:

        await update.effective_message.reply_text(
            "❌ Invalid chat ID."
        )

        return

    await chats_col.update_one(

        {
            "chat_id": chat_id
        },

        {
            "$set": {
                "enabled": False
            }
        }
    )

    await update.effective_message.reply_text(
        "✅ Chat removed from broadcast list."
    )


@admin_only
async def chats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    rows = await get_registered_chats()

    if not rows:

        await update.effective_message.reply_text(
            "❌ No registered groups/channels."
        )

        return

    lines = [
        "📋 REGISTERED CHATS\n"
    ]

    for row in rows:

        lines.append(

            f"• {row.get('title', '-')}\n"
            f"  ID: {row['chat_id']}\n"
            f"  Type: {row.get('type', '-')}\n"

        )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# SETUP
# ============================================================

async def begin_setup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str
):

    reset_setup(context)

    context.user_data["setup"] = {

        "mode": mode,

        "step": "content",

        "photo_file_id": None,

        "text": None,

        "button_text": None,

        "button_url": None,

        "interval": None
    }

    await update.effective_message.reply_text(

        f"🚀 {mode.upper()} SETUP\n\n"

        "🖼️ Send PHOTO with caption\n"
        "OR\n"
        "📝 Send TEXT only\n\n"

        "Then I will ask for button.\n\n"

        "Use /cancel to stop."

    )


# ============================================================
# BROADCAST
# ============================================================

@admin_only
async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await begin_setup(
        update,
        context,
        "broadcast"
    )


# ============================================================
# USER BROADCAST
# ============================================================

@admin_only
async def ubroadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await begin_setup(
        update,
        context,
        "ubroadcast"
    )


# ============================================================
# SCHEDULE
# ============================================================

@admin_only
async def schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await begin_setup(
        update,
        context,
        "schedule"
    )


# ============================================================
# CAPTURE CONTENT
# ============================================================

async def capture_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    if setup.get("step") != "content":
        return

    message = update.effective_message

    # PHOTO
    if message.photo:

        setup["photo_file_id"] = (
            message.photo[-1].file_id
        )

        setup["text"] = (
            message.caption or ""
        )

    # TEXT
    elif message.text:

        if message.text.startswith("/"):
            return

        setup["photo_file_id"] = None

        setup["text"] = message.text

    else:

        return

    setup["step"] = "button"

    await message.reply_text(

        "🔘 BUTTON SETUP\n\n"

        "Send button like this:\n\n"

        "Button Text | https://example.com\n\n"

        "OR\n\n"

        "skip\n\n"

        "Example:\n"
        "Join Now | https://t.me/example"

    )


# ============================================================
# CAPTURE BUTTON
# ============================================================

async def capture_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    if setup.get("step") != "button":
        return

    text = (
        update.effective_message.text or ""
    ).strip()

    # NO BUTTON
    if text.lower() == "skip":

        setup["button_text"] = None
        setup["button_url"] = None

    else:

        if "|" not in text:

            await update.effective_message.reply_text(

                "❌ Wrong format.\n\n"

                "Use:\n"
                "Button Text | https://example.com"

            )

            return

        button_text, button_url = (
            x.strip()
            for x in text.split("|", 1)
        )

        if not button_text:

            await update.effective_message.reply_text(
                "❌ Button text is empty."
            )

            return

        if not (
            button_url.startswith("https://")
            or button_url.startswith("http://")
            or button_url.startswith("tg://")
        ):

            await update.effective_message.reply_text(
                "❌ Invalid URL."
            )

            return

        setup["button_text"] = (
            button_text[:64]
        )

        setup["button_url"] = button_url

    # ========================================================
    # MANUAL BROADCAST
    # ========================================================

    if setup["mode"] == "broadcast":

        await do_manual_broadcast(
            update,
            context
        )

        return

    # ========================================================
    # USER BROADCAST
    # ========================================================

    if setup["mode"] == "ubroadcast":

        await do_user_broadcast(
            update,
            context
        )

        return

    # ========================================================
    # SCHEDULE
    # ========================================================

    if setup["mode"] == "schedule":

        setup["step"] = "interval"

        await update.effective_message.reply_text(

            "⏱️ SELECT INTERVAL\n\n"

            "Send one:\n\n"

            "10m\n"
            "15m\n"
            "30m\n"
            "1h"

        )


# ============================================================
# MANUAL GROUP/CHANNEL BROADCAST
# ============================================================

async def do_manual_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    setup = context.user_data.get(
        "setup"
    )

    payload = {

        "photo_file_id":
            setup["photo_file_id"],

        "text":
            setup["text"] or "",

        "button_text":
            setup["button_text"],

        "button_url":
            setup["button_url"]

    }

    chats_list = await get_registered_chats()

    total = len(chats_list)

    if total == 0:

        reset_setup(context)

        await update.effective_message.reply_text(

            "❌ No groups/channels registered.\n\n"

            "Use:\n"
            "/addchat <chat_id>"

        )

        return

    progress = await update.effective_message.reply_text(

        "📢 BROADCAST STARTING...\n\n"

        f"📦 Total: {total}\n"
        "✅ Sent: 0\n"
        "⏳ Remaining: "
        f"{total}\n"
        "❌ Failed: 0\n"
        "📊 Progress: 0%"

    )

    sent = 0
    failed = 0

    for index, chat in enumerate(
        chats_list,
        start=1
    ):

        chat_id = chat["chat_id"]

        try:

            await send_payload(
                context.bot,
                chat_id,
                payload
            )

            sent += 1

        except RetryAfter as e:

            wait_seconds = (
                int(
                    getattr(
                        e,
                        "retry_after",
                        1
                    )
                ) + 2
            )

            await asyncio.sleep(
                wait_seconds
            )

            try:

                await send_payload(
                    context.bot,
                    chat_id,
                    payload
                )

                sent += 1

            except Exception:

                failed += 1

        except Exception as e:

            failed += 1

            log.warning(
                "Broadcast failed %s: %s",
                chat_id,
                e
            )

        # Small safe delay
        await asyncio.sleep(
            0.3
        )

        # LIVE PROGRESS
        if (
            index % PROGRESS_EVERY == 0
            or index == total
        ):

            remaining = (
                total - index
            )

            percent = round(
                index / total * 100,
                1
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

    reset_setup(context)

    success_rate = (
        sent / total * 100
        if total
        else 0
    )

    await progress.edit_text(

        "✅ BROADCAST COMPLETED\n\n"

        f"📦 Total: {total}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"📈 Success: {success_rate:.1f}%"

    )


# ============================================================
# USER BROADCAST
# ============================================================

async def do_user_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    setup = context.user_data.get(
        "setup"
    )

    payload = {

        "photo_file_id":
            setup["photo_file_id"],

        "text":
            setup["text"] or "",

        "button_text":
            setup["button_text"],

        "button_url":
            setup["button_url"]

    }

    total = await users_col.count_documents(
        {
            "blocked": {
                "$ne": True
            }
        }
    )

    if total == 0:

        reset_setup(context)

        await update.effective_message.reply_text(
            "❌ No users found."
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
        }
    ).sort(
        "_id",
        1
    )

    async for row in cursor:

        user_id = int(
            row["user_id"]
        )

        try:

            await safe_send_user(
                context.bot,
                user_id,
                payload
            )

            sent += 1

        except Exception as e:

            failed += 1

            error_text = str(
                e
            ).lower()

            # Mark permanently unavailable users.
            if (
                "blocked" in error_text
                or "deactivated" in error_text
                or "chat not found" in error_text
                or "user is deactivated" in error_text
            ):

                await users_col.update_one(

                    {
                        "user_id": user_id
                    },

                    {
                        "$set": {
                            "blocked": True,
                            "blocked_at":
                                datetime.now(
                                    timezone.utc
                                )
                        }
                    }
                )

        processed += 1

        # Conservative delay
        await asyncio.sleep(
            USER_SEND_DELAY
        )

        # LIVE PROGRESS
        if (
            processed % PROGRESS_EVERY == 0
            or processed == total
        ):

            remaining = max(
                total - processed,
                0
            )

            percent = round(
                processed / total * 100,
                1
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

    reset_setup(context)

    success_rate = (
        sent / total * 100
        if total
        else 0
    )

    await progress.edit_text(

        "✅ USER BROADCAST COMPLETED\n\n"

        f"👥 Total: {total}\n"
        f"✅ Successfully Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"📈 Success Rate: {success_rate:.1f}%"

    )


# ============================================================
# SCHEDULE INTERVALS
# ============================================================

INTERVALS = {

    "10m": 10 * 60,

    "15m": 15 * 60,

    "30m": 30 * 60,

    "1h": 60 * 60

}


# ============================================================
# CAPTURE SCHEDULE INTERVAL
# ============================================================

async def capture_interval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    setup = context.user_data.get(
        "setup"
    )

    if not setup:
        return

    if setup.get("step") != "interval":
        return

    value = (
        update.effective_message.text or ""
    ).strip().lower()

    if value not in INTERVALS:

        await update.effective_message.reply_text(

            "❌ Invalid interval.\n\n"

            "Choose:\n"
            "10m\n"
            "15m\n"
            "30m\n"
            "1h"

        )

        return

    setup["interval"] = (
        INTERVALS[value]
    )

    payload = {

        "photo_file_id":
            setup["photo_file_id"],

        "text":
            setup["text"] or "",

        "button_text":
            setup["button_text"],

        "button_url":
            setup["button_url"]

    }

    doc = {

        "interval":
            setup["interval"],

        "interval_label":
            value,

        "payload":
            payload,

        "enabled":
            True,

        "last_messages":
            {},

        "created_at":
            datetime.now(
                timezone.utc
            ),

        "updated_at":
            datetime.now(
                timezone.utc
            )

    }

    result = await schedules_col.insert_one(
        doc
    )

    schedule_id = str(
        result.inserted_id
    )

    # Send immediately
    await run_schedule(
        schedule_id,
        context.application
    )

    # Then repeat
    context.application.job_queue.run_repeating(

        scheduled_job,

        interval=setup["interval"],

        first=setup["interval"],

        data={
            "schedule_id":
                schedule_id
        },

        name=f"schedule:{schedule_id}"

    )

    reset_setup(context)

    await update.effective_message.reply_text(

        "✅ AUTO SCHEDULE CREATED\n\n"

        f"🆔 ID: {schedule_id}\n"
        f"⏱ Interval: {value}\n"
        "🔄 Auto Delete: ON\n"
        "👥 Groups + Channels: Enabled Chats\n\n"

        "The first message was sent immediately."

    )


# ============================================================
# DELETE OLD SCHEDULE MESSAGE
# ============================================================

async def delete_previous_message(
    bot,
    chat_id,
    message_id
):

    if not message_id:
        return

    try:

        await bot.delete_message(

            chat_id=chat_id,

            message_id=message_id

        )

    except Exception as e:

        log.info(
            "Old message already unavailable %s/%s: %s",
            chat_id,
            message_id,
            e
        )


# ============================================================
# RUN SCHEDULE
# ============================================================

async def run_schedule(
    schedule_id,
    application
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
            "enabled": True
        }

    )

    if not doc:
        return

    payload = doc["payload"]

    chats_list = await get_registered_chats()

    old_messages = doc.get(
        "last_messages",
        {}
    )

    new_messages = dict(
        old_messages
    )

    for chat in chats_list:

        chat_id = chat["chat_id"]

        key = str(
            chat_id
        )

        try:

            # Delete old message
            await delete_previous_message(

                application.bot,

                chat_id,

                old_messages.get(
                    key
                )

            )

            # Send new message
            message = await send_payload(

                application.bot,

                chat_id,

                payload

            )

            new_messages[key] = (
                message.message_id
            )

        except RetryAfter as e:

            wait_seconds = (
                int(
                    getattr(
                        e,
                        "retry_after",
                        1
                    )
                ) + 2
            )

            await asyncio.sleep(
                wait_seconds
            )

            try:

                message = await send_payload(

                    application.bot,

                    chat_id,

                    payload

                )

                new_messages[key] = (
                    message.message_id
                )

            except Exception as retry_error:

                log.warning(
                    "Schedule retry failed %s: %s",
                    chat_id,
                    retry_error
                )

        except Exception as e:

            log.warning(

                "Schedule %s failed for %s: %s",

                schedule_id,

                chat_id,

                e

            )

        await asyncio.sleep(
            0.3
        )

    await schedules_col.update_one(

        {
            "_id": object_id
        },

        {
            "$set": {

                "last_messages":
                    new_messages,

                "updated_at":
                    datetime.now(
                        timezone.utc
                    )

            }
        }

    )


# ============================================================
# SCHEDULE JOB
# ============================================================

async def scheduled_job(
    context: ContextTypes.DEFAULT_TYPE
):

    schedule_id = (
        context.job.data["schedule_id"]
    )

    await run_schedule(
        schedule_id,
        context.application
    )


# ============================================================
# RESTORE SCHEDULES
# ============================================================

async def restore_schedules(
    application
):

    rows = await schedules_col.find(
        {
            "enabled": True
        }
    ).to_list(
        length=1000
    )

    for doc in rows:

        schedule_id = str(
            doc["_id"]
        )

        interval = int(
            doc["interval"]
        )

        # Remove duplicate jobs
        for job in application.job_queue.get_jobs_by_name(
            f"schedule:{schedule_id}"
        ):

            job.schedule_removal()

        application.job_queue.run_repeating(

            scheduled_job,

            interval=interval,

            first=interval,

            data={
                "schedule_id":
                    schedule_id
            },

            name=f"schedule:{schedule_id}"

        )

    log.info(
        "Restored %d schedules",
        len(rows)
    )


# ============================================================
# LIST SCHEDULES
# ============================================================

@admin_only
async def schedules(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    rows = await schedules_col.find().sort(
        "created_at",
        -1
    ).to_list(
        length=100
    )

    if not rows:

        await update.effective_message.reply_text(
            "❌ No schedules."
        )

        return

    lines = [
        "🔄 ACTIVE SCHEDULES\n"
    ]

    for doc in rows:

        sid = str(
            doc["_id"]
        )

        status = (
            "🟢 ON"
            if doc.get("enabled")
            else "⏸ OFF"
        )

        lines.append(

            f"{status}\n"
            f"🆔 {sid}\n"
            f"⏱ {doc.get('interval_label', '-')}\n"

        )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# PAUSE
# ============================================================

@admin_only
async def pause(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
            "❌ Invalid schedule ID."
        )

        return

    result = await schedules_col.update_one(

        {
            "_id": object_id
        },

        {
            "$set": {
                "enabled": False,
                "updated_at":
                    datetime.now(
                        timezone.utc
                    )
            }
        }

    )

    for job in context.application.job_queue.get_jobs_by_name(
        f"schedule:{sid}"
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
            "❌ Invalid schedule ID."
        )

        return

    doc = await schedules_col.find_one(
        {
            "_id": object_id
        }
    )

    if not doc:

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
                "updated_at":
                    datetime.now(
                        timezone.utc
                    )
            }
        }

    )

    for job in context.application.job_queue.get_jobs_by_name(
        f"schedule:{sid}"
    ):

        job.schedule_removal()

    context.application.job_queue.run_repeating(

        scheduled_job,

        interval=int(
            doc["interval"]
        ),

        first=1,

        data={
            "schedule_id":
                sid
        },

        name=f"schedule:{sid}"

    )

    await update.effective_message.reply_text(
        "▶️ Schedule resumed."
    )


# ============================================================
# DELETE SCHEDULE
# ============================================================

@admin_only
async def delete_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
            "❌ Invalid schedule ID."
        )

        return

    doc = await schedules_col.find_one(
        {
            "_id": object_id
        }
    )

    if not doc:

        await update.effective_message.reply_text(
            "❌ Schedule not found."
        )

        return

    # Delete currently visible scheduled messages
    last_messages = doc.get(
        "last_messages",
        {}
    )

    for chat_id, message_id in last_messages.items():

        try:

            await context.bot.delete_message(

                chat_id=int(chat_id),

                message_id=int(message_id)

            )

        except Exception:
            pass

    await schedules_col.delete_one(
        {
            "_id": object_id
        }
    )

    for job in context.application.job_queue.get_jobs_by_name(
        f"schedule:{sid}"
    ):

        job.schedule_removal()

    await update.effective_message.reply_text(

        "🗑 Schedule deleted.\n"
        "Current scheduled messages removed."

    )


# ============================================================
# CANCEL
# ============================================================

@admin_only
async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    reset_setup(context)

    await update.effective_message.reply_text(
        "❌ Setup cancelled."
    )


# ============================================================
# SETUP ROUTER
# ============================================================

async def setup_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
            context
        )

    elif step == "button":

        await capture_button(
            update,
            context
        )

    elif step == "interval":

        await capture_interval(
            update,
            context
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE
):

    log.exception(
        "Unhandled exception:",
        exc_info=context.error
    )


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    application
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

    db = mongo[
        DB_NAME
    ]

    users_col = db[
        "users"
    ]

    chats_col = db[
        "chats"
    ]

    schedules_col = db[
        "schedules"
    ]

    # Indexes
    await users_col.create_index(
        "user_id",
        unique=True
    )

    await chats_col.create_index(
        "chat_id",
        unique=True
    )

    await schedules_col.create_index(
        "enabled"
    )

    await restore_schedules(
        application
    )

    log.info(
        "Bot initialized successfully."
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def post_shutdown(
    application
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

        .token(
            BOT_TOKEN
        )

        .post_init(
            post_init
        )

        .post_shutdown(
            post_shutdown
        )

        .build()

    )

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "users",
            users
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast",
            broadcast
        )
    )

    application.add_handler(
        CommandHandler(
            "ubroadcast",
            ubroadcast
        )
    )

    application.add_handler(
        CommandHandler(
            "schedule",
            schedule
        )
    )

    application.add_handler(
        CommandHandler(
            "schedules",
            schedules
        )
    )

    application.add_handler(
        CommandHandler(
            "pause",
            pause
        )
    )

    application.add_handler(
        CommandHandler(
            "resume",
            resume
        )
    )

    application.add_handler(
        CommandHandler(
            "delete_schedule",
            delete_schedule
        )
    )

    application.add_handler(
        CommandHandler(
            "addchat",
            addchat
        )
    )

    application.add_handler(
        CommandHandler(
            "removechat",
            removechat
        )
    )

    application.add_handler(
        CommandHandler(
            "chats",
            chats
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel
        )
    )

    # ========================================================
    # SETUP MESSAGE HANDLER
    # ========================================================

    application.add_handler(

        MessageHandler(

            (
                filters.PHOTO
                |
                (
                    filters.TEXT
                    &
                    ~filters.COMMAND
                )
            ),

            setup_router

        )

    )

    application.add_error_handler(
        error_handler
    )

    log.info(
        "Starting Telegram polling..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
