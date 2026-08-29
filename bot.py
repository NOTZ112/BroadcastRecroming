import os
import asyncio
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import FloodWait, RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

DB_NAME = os.getenv("DB_NAME", "broadcast_scheduler")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("broadcast_scheduler")

mongo = None
db = None
chats_col = None
schedules_col = None
users_col = None


# One-time message sent to a user on their FIRST /start only.
WELCOME_TEXT = (
    "👋 Welcome!\n\n"
    "🎬 പുതിയ വീഡിയോകൾ\n"
    "👥 നല്ല ഗ്രൂപ്പുകൾ\n"
    "📢 പുതിയ ചാനലുകൾ\n\n"
    "എല്ലാം ഇവിടെ ലഭിക്കും. ❤️"
)

# Conservative user-broadcast settings.
USER_SEND_DELAY = float(os.getenv("USER_SEND_DELAY", "0.8"))
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "20"))
MAX_SEND_RETRIES = 3

# ============================================================
# HELPERS
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, context)
    return wrapper


async def get_registered_chats():
    return await chats_col.find({"enabled": True}).to_list(length=1000)


async def delete_previous_message(chat_id: int, message_id: int | None, bot):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        # Message may already be deleted / inaccessible.
        log.info("Could not delete old message %s/%s: %s", chat_id, message_id, e)


async def send_payload(bot, chat_id: int, payload: dict):
    keyboard = None
    button_text = payload.get("button_text")
    button_url = payload.get("button_url")

    if button_text and button_url:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(button_text, url=button_url)]]
        )

    photo_file_id = payload.get("photo_file_id")
    text = payload.get("text") or ""

    if photo_file_id:
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_id,
            caption=text[:1024] if text else None,
            reply_markup=keyboard
        )
    else:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text[:4096],
            reply_markup=keyboard
        )

    return msg


# ============================================================
# START / HELP
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    # Every /start is counted as a known user, but the welcome is sent only once.
    existing = await users_col.find_one({"user_id": user.id})

    if existing:
        await users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "name": user.full_name,
                "username": user.username,
                "last_seen_at": datetime.now(timezone.utc)
            }}
        )
        return

    await users_col.insert_one({
        "user_id": user.id,
        "name": user.full_name,
        "username": user.username,
        "first_started_at": datetime.now(timezone.utc),
        "last_seen_at": datetime.now(timezone.utc),
        "welcome_sent": True,
        "blocked": False
    })

    # One-time welcome message. Change WELCOME_TEXT below if desired.
    await update.effective_message.reply_text(WELCOME_TEXT)



async def safe_send_user(bot, user_id: int, payload: dict):
    """Send to one user with conservative pacing and FloodWait handling."""
    last_error = None

    for attempt in range(MAX_SEND_RETRIES + 1):
        try:
            msg = await send_payload(bot, user_id, payload)
            return msg

        except FloodWait as e:
            last_error = e
            wait_seconds = int(getattr(e, "retry_after", 1)) + 2
            log.warning("FloodWait: sleeping %ss", wait_seconds)
            await asyncio.sleep(wait_seconds)

        except RetryAfter as e:
            last_error = e
            wait_seconds = int(getattr(e, "retry_after", 1)) + 1
            await asyncio.sleep(wait_seconds)

        except (TimedOut, NetworkError) as e:
            last_error = e
            if attempt >= MAX_SEND_RETRIES:
                break
            await asyncio.sleep(2 ** attempt)

        except Exception as e:
            # Blocked/deleted users and other permanent failures are not retried.
            last_error = e
            break

    raise last_error if last_error else RuntimeError("Unknown send failure")


@admin_only
async def ubroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await begin_setup(update, context, "ubroadcast")


async def do_user_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data.get("setup")

    payload = {
        "photo_file_id": setup["photo_file_id"],
        "text": setup["text"] or "",
        "button_text": setup["button_text"],
        "button_url": setup["button_url"]
    }

    total = await users_col.count_documents({"blocked": {"$ne": True}})
    if total == 0:
        reset_setup(context)
        await update.effective_message.reply_text("❌ No users found.")
        return

    progress = await update.effective_message.reply_text(
        "📢 USER BROADCAST STARTING...\n\n"
        f"👥 Total: {total}\n"
        "✅ Sent: 0\n"
        "⏳ Remaining: 0\n"
        "❌ Failed: 0\n"
        "📊 Progress: 0%"
    )

    sent = 0
    failed = 0
    processed = 0

    cursor = users_col.find(
        {"blocked": {"$ne": True}},
        {"user_id": 1}
    ).sort("_id", 1)

    async for row in cursor:
        user_id = int(row["user_id"])

        try:
            await safe_send_user(context.bot, user_id, payload)
            sent += 1
        except Exception as e:
            failed += 1
            # Telegram errors such as blocked users are recorded so they
            # are skipped on future user broadcasts.
            text = str(e).lower()
            if "blocked" in text or "deactivated" in text or "chat not found" in text:
                await users_col.update_one(
                    {"user_id": user_id},
                    {"$set": {"blocked": True, "blocked_at": datetime.now(timezone.utc)}}
                )

        processed += 1

        # Conservative pacing. FloodWait still has automatic handling above.
        await asyncio.sleep(USER_SEND_DELAY)

        if processed % PROGRESS_EVERY == 0 or processed == total:
            remaining = max(total - processed, 0)
            percent = round((processed / total) * 100, 1)

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

    await progress.edit_text(
        "✅ USER BROADCAST COMPLETED\n\n"
        f"👥 Total: {total}\n"
        f"✅ Successfully Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"📊 Success Rate: {(sent / total * 100):.1f}%"
    )



@admin_only
async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = await users_col.count_documents({})
    active = await users_col.count_documents({"blocked": {"$ne": True}})
    blocked = await users_col.count_documents({"blocked": True})

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
async def addchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/addchat -1001234567890"
        )
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid chat ID.")
        return

    try:
        chat = await context.bot.get_chat(chat_id)
        await chats_col.update_one(
            {"chat_id": chat_id},
            {"$set": {
                "chat_id": chat_id,
                "title": chat.title or str(chat_id),
                "type": chat.type,
                "enabled": True,
                "updated_at": datetime.now(timezone.utc)
            }},
            upsert=True
        )
        await update.effective_message.reply_text(
            f"✅ Added\n\n"
            f"Title: {chat.title or '-'}\n"
            f"Type: {chat.type}\n"
            f"ID: {chat_id}"
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ Could not add chat.\n\n{e}\n\n"
            "Make sure the bot is inside the group/channel and has permission to post."
        )


@admin_only
async def removechat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/removechat -1001234567890"
        )
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid chat ID.")
        return

    await chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"enabled": False}}
    )
    await update.effective_message.reply_text("✅ Chat disabled.")


@admin_only
async def chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_registered_chats()

    if not rows:
        await update.effective_message.reply_text("No groups/channels registered.")
        return

    lines = ["📋 Registered Chats\n"]
    for r in rows:
        lines.append(
            f"• {r.get('title', '-')}\n"
            f"  ID: `{r['chat_id']}` | {r.get('type', '-')}"
        )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown"
    )


# ============================================================
# SETUP STATE
# ============================================================
def reset_setup(context):
    context.user_data.pop("setup", None)


@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_setup(context)
    await update.effective_message.reply_text("❌ Setup cancelled.")


async def begin_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
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
        "Now send the content:\n\n"
        "• Send a PHOTO with caption, OR\n"
        "• Send TEXT only\n\n"
        "After that I'll ask for the button.\n"
        "Use /cancel to stop."
    )


@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await begin_setup(update, context, "broadcast")


@admin_only
async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await begin_setup(update, context, "schedule")


# ============================================================
# CONTENT CAPTURE
# ============================================================
@admin_only
async def capture_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data.get("setup")
    if not setup or setup.get("step") != "content":
        return

    msg = update.effective_message

    if msg.photo:
        setup["photo_file_id"] = msg.photo[-1].file_id
        setup["text"] = msg.caption or ""
    elif msg.text and not msg.text.startswith("/"):
        setup["photo_file_id"] = None
        setup["text"] = msg.text
    else:
        return

    setup["step"] = "button"
    await msg.reply_text(
        "🔘 Button setup\n\n"
        "Send button as:\n"
        "`Button Text | https://example.com`\n\n"
        "Or send `skip` for no button.",
        parse_mode="Markdown"
    )


@admin_only
async def capture_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data.get("setup")
    if not setup or setup.get("step") != "button":
        return

    text = (update.effective_message.text or "").strip()

    if text.lower() == "skip":
        setup["button_text"] = None
        setup["button_url"] = None
    else:
        if "|" not in text:
            await update.effective_message.reply_text(
                "❌ Format:\nButton Text | https://example.com"
            )
            return

        button_text, button_url = [x.strip() for x in text.split("|", 1)]

        if not button_text or not (
            button_url.startswith("https://") or
            button_url.startswith("http://") or
            button_url.startswith("tg://")
        ):
            await update.effective_message.reply_text(
                "❌ Invalid button. Example:\nJoin Now | https://t.me/example"
            )
            return

        setup["button_text"] = button_text[:64]
        setup["button_url"] = button_url

    if setup["mode"] == "broadcast":
        await do_manual_broadcast(update, context)
    elif setup["mode"] == "ubroadcast":
        await do_user_broadcast(update, context)
    else:
        setup["step"] = "interval"
        await update.effective_message.reply_text(
            "⏱️ Select interval by sending one of these:\n\n"
            "`15m`\n"
            "`30m`\n"
            "`1h`",
            parse_mode="Markdown"
        )


# ============================================================
# INTERVAL CAPTURE
# ============================================================
INTERVALS = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60
}


@admin_only
async def capture_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data.get("setup")
    if not setup or setup.get("step") != "interval":
        return

    value = (update.effective_message.text or "").strip().lower()

    if value not in INTERVALS:
        await update.effective_message.reply_text(
            "❌ Choose: 15m, 30m, or 1h"
        )
        return

    setup["interval"] = INTERVALS[value]

    payload = {
        "photo_file_id": setup["photo_file_id"],
        "text": setup["text"] or "",
        "button_text": setup["button_text"],
        "button_url": setup["button_url"]
    }

    doc = {
        "interval": setup["interval"],
        "interval_label": value,
        "payload": payload,
        "enabled": True,
        "last_messages": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }

    result = await schedules_col.insert_one(doc)
    schedule_id = str(result.inserted_id)

    # Run immediately, then repeat.
    await run_schedule(schedule_id, context.application)

    context.application.job_queue.run_repeating(
        scheduled_job,
        interval=setup["interval"],
        first=setup["interval"],
        data={"schedule_id": schedule_id},
        name=f"schedule:{schedule_id}"
    )

    reset_setup(context)

    await update.effective_message.reply_text(
        f"✅ AUTO SCHEDULE CREATED\n\n"
        f"🆔 ID: `{schedule_id}`\n"
        f"⏱ Interval: {value}\n"
        f"🔄 Auto delete old message: ON\n"
        f"👥 Groups + Channels: all enabled chats\n\n"
        f"New message will replace the previous one each cycle.",
        parse_mode="Markdown"
    )


# ============================================================
# MANUAL BROADCAST
# ============================================================
async def do_manual_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data.get("setup")

    payload = {
        "photo_file_id": setup["photo_file_id"],
        "text": setup["text"] or "",
        "button_text": setup["button_text"],
        "button_url": setup["button_url"]
    }

    chats_list = await get_registered_chats()

    if not chats_list:
        reset_setup(context)
        await update.effective_message.reply_text(
            "❌ No groups/channels registered.\n"
            "Use /addchat <chat_id> first."
        )
        return

    sent = 0
    failed = 0

    for chat in chats_list:
        try:
            await send_payload(context.bot, chat["chat_id"], payload)
            sent += 1
        except Exception as e:
            failed += 1
            log.warning("Broadcast failed to %s: %s", chat["chat_id"], e)

    reset_setup(context)

    await update.effective_message.reply_text(
        f"📢 BROADCAST COMPLETE\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total: {len(chats_list)}"
    )


# ============================================================
# RECURRING SCHEDULE
# ============================================================
async def run_schedule(schedule_id: str, application: Application):
    from bson import ObjectId

    try:
        obj_id = ObjectId(schedule_id)
    except Exception:
        return

    doc = await schedules_col.find_one({"_id": obj_id, "enabled": True})
    if not doc:
        return

    payload = doc["payload"]
    chats_list = await get_registered_chats()

    last_messages = doc.get("last_messages", {})
    new_last_messages = dict(last_messages)

    for chat in chats_list:
        chat_id = chat["chat_id"]
        key = str(chat_id)

        try:
            old_message_id = last_messages.get(key)
            await delete_previous_message(
                chat_id, old_message_id, application.bot
            )

            new_msg = await send_payload(application.bot, chat_id, payload)
            new_last_messages[key] = new_msg.message_id

        except Exception as e:
            log.warning(
                "Schedule %s failed for %s: %s",
                schedule_id, chat_id, e
            )

    await schedules_col.update_one(
        {"_id": obj_id},
        {"$set": {
            "last_messages": new_last_messages,
            "updated_at": datetime.now(timezone.utc)
        }}
    )


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    schedule_id = context.job.data["schedule_id"]
    await run_schedule(schedule_id, context.application)


async def restore_schedules(application: Application):
    rows = await schedules_col.find({"enabled": True}).to_list(length=1000)

    for doc in rows:
        schedule_id = str(doc["_id"])
        interval = int(doc["interval"])

        # Remove duplicate restored jobs if any.
        for job in application.job_queue.get_jobs_by_name(
            f"schedule:{schedule_id}"
        ):
            job.schedule_removal()

        application.job_queue.run_repeating(
            scheduled_job,
            interval=interval,
            first=interval,
            data={"schedule_id": schedule_id},
            name=f"schedule:{schedule_id}"
        )

    log.info("Restored %d active schedules", len(rows))


# ============================================================
# SCHEDULE ADMIN
# ============================================================
@admin_only
async def schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await schedules_col.find().sort("created_at", -1).to_list(length=100)

    if not rows:
        await update.effective_message.reply_text("No schedules.")
        return

    lines = ["🔄 SCHEDULES\n"]

    for doc in rows:
        sid = str(doc["_id"])
        status = "🟢 ON" if doc.get("enabled") else "⏸ OFF"
        lines.append(
            f"{status} `{sid}`\n"
            f"⏱ {doc.get('interval_label', doc.get('interval'))}\n"
        )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown"
    )


def find_job(application, schedule_id):
    return application.job_queue.get_jobs_by_name(f"schedule:{schedule_id}")


@admin_only
async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("/pause <schedule_id>")
        return

    sid = context.args[0]

    from bson import ObjectId
    try:
        oid = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text("❌ Invalid schedule ID.")
        return

    result = await schedules_col.update_one(
        {"_id": oid},
        {"$set": {"enabled": False, "updated_at": datetime.now(timezone.utc)}}
    )

    for job in find_job(context.application, sid):
        job.schedule_removal()

    await update.effective_message.reply_text(
        "⏸ Schedule paused." if result.matched_count else "❌ Schedule not found."
    )


@admin_only
async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("/resume <schedule_id>")
        return

    sid = context.args[0]

    from bson import ObjectId
    try:
        oid = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text("❌ Invalid schedule ID.")
        return

    doc = await schedules_col.find_one({"_id": oid})
    if not doc:
        await update.effective_message.reply_text("❌ Schedule not found.")
        return

    await schedules_col.update_one(
        {"_id": oid},
        {"$set": {"enabled": True, "updated_at": datetime.now(timezone.utc)}}
    )

    for job in find_job(context.application, sid):
        job.schedule_removal()

    context.application.job_queue.run_repeating(
        scheduled_job,
        interval=int(doc["interval"]),
        first=1,
        data={"schedule_id": sid},
        name=f"schedule:{sid}"
    )

    await update.effective_message.reply_text("▶️ Schedule resumed.")


@admin_only
async def delete_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "/delete_schedule <schedule_id>"
        )
        return

    sid = context.args[0]

    from bson import ObjectId
    try:
        oid = ObjectId(sid)
    except Exception:
        await update.effective_message.reply_text("❌ Invalid schedule ID.")
        return

    doc = await schedules_col.find_one({"_id": oid})
    if not doc:
        await update.effective_message.reply_text("❌ Schedule not found.")
        return

    # Delete currently displayed scheduled messages.
    last_messages = doc.get("last_messages", {})
    for chat_id_str, message_id in last_messages.items():
        try:
            await context.bot.delete_message(
                chat_id=int(chat_id_str),
                message_id=int(message_id)
            )
        except Exception:
            pass

    await schedules_col.delete_one({"_id": oid})

    for job in find_job(context.application, sid):
        job.schedule_removal()

    await update.effective_message.reply_text(
        "🗑 Schedule deleted and its current messages removed."
    )


# ============================================================
# ERROR HANDLER
# ============================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================
async def post_init(application: Application):
    global mongo, db, chats_col, schedules_col

    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing.")

    mongo = AsyncIOMotorClient(MONGO_URI)
    db = mongo[DB_NAME]
    chats_col = db["chats"]
    schedules_col = db["schedules"]
    users_col = db["users"]

    await chats_col.create_index("chat_id", unique=True)
    await users_col.create_index("user_id", unique=True)
    await schedules_col.create_index("enabled")

    await restore_schedules(application)
    log.info("Bot initialized.")


async def post_shutdown(application: Application):
    global mongo
    if mongo:
        mongo.close()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is missing.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ubroadcast", ubroadcast))
    app.add_handler(CommandHandler("users", users))
    app.add_handler(CommandHandler("schedule", schedule))
    app.add_handler(CommandHandler("schedules", schedules))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("delete_schedule", delete_schedule))
    app.add_handler(CommandHandler("addchat", addchat))
    app.add_handler(CommandHandler("removechat", removechat))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler("cancel", cancel))

    # Setup messages. Content first, then button, then interval.
    app.add_handler(
        MessageHandler(
            filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
            setup_router
        )
    )

    app.add_error_handler(error_handler)

    log.info("Starting polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


async def setup_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


if __name__ == "__main__":
    main()
