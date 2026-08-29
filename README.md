# Telegram Broadcast + Auto Schedule Bot — Render Ready

## Features

### 1. `/broadcast`
Admin-only broadcast to all enabled registered groups and channels.

Supports:
- Photo + caption
- Text only
- Optional inline URL button
- Live progress updates every 20 destinations
- FloodWait-aware sending

### 2. `/ubroadcast`
Admin-only user broadcast.

It sends to users who have started the bot with `/start`.

Supports:
- Photo + caption
- Text only
- Optional inline URL button
- Live progress updates
- Conservative send pacing
- Automatic FloodWait handling
- Blocked/deactivated users are marked and skipped later

### 3. `/start`
A user is saved in MongoDB the first time they start the bot.

The welcome message is sent **only once**.
If the same user sends `/start` again, no welcome message is sent.

### 4. `/users`
Shows:
- Total users
- Active users
- Blocked users

### 5. `/schedule`
Recurring auto-refresh message for groups/channels:
- 15 minutes
- 30 minutes
- 1 hour

The bot attempts to delete the previous scheduled message before sending its replacement.

Schedules are stored in MongoDB and restored after restart/redeploy.

## Files

- `bot.py`
- `requirements.txt`
- `.env.example`
- `Procfile`
- `.python-version`
- `README.md`

## Render

Create a **Background Worker**.

Build command:
`pip install -r requirements.txt`

Start command:
`python bot.py`

Environment variables:
- `BOT_TOKEN`
- `MONGO_URI`
- `ADMIN_IDS`
- `DB_NAME` (optional)
- `USER_SEND_DELAY` (optional, default `0.8`)
- `PROGRESS_EVERY` (optional, default `20`)

Do not put real secrets into `.env.example`.

## Telegram permissions

For groups:
- Add the bot.
- Give it permission to send messages and delete messages.

For channels:
- Add the bot as administrator.
- Give it permission to post and delete messages.

## Register groups/channels

`/addchat -1001234567890`

Then:

`/chats`

## User broadcast

`/ubroadcast`

Then:
1. Send a photo with caption OR text.
2. Send `Button Text | https://example.com`
3. Or send `skip`.

The bot displays live sent/remaining/failed counts while sending.

## Manual broadcast

`/broadcast`

Same content flow as user broadcast, but it targets registered groups/channels.

## Recurring schedule

`/schedule`

Send the content, button (or `skip`), then:

`15m`

or:

`30m`

or:

`1h`

The first scheduled message is sent immediately.

## Schedule controls

`/schedules`

`/pause SCHEDULE_ID`

`/resume SCHEDULE_ID`

`/delete_schedule SCHEDULE_ID`
