import os
import asyncio
from datetime import datetime, timedelta
import tempfile
from pathlib import Path

from pymongo import MongoClient
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, DeleteChatUserRequest


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
ApplicationBuilder,
CommandHandler,
ContextTypes,
CallbackQueryHandler,
MessageHandler,
filters,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from bson.objectid import ObjectId


# ================= CONFIG =================

API_ID = int(os.environ.get("API_ID", 21189715))          # Your Telegram API ID
API_HASH = os.environ.get("API_HASH", "988a9111105fd2f0c5e21c2c2449edfd")         # Your Telegram API Hash
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8529534400:AAGEB_lmsavkS3ptGB0dzoL4itnHqCsQ_LE")       # Your Bot Token
# Multiple Admin IDs (add more comma-separated)
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "7932059238,8582267655").split(",")]

# ===== MONGODB =====
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://codexkairnex:gm6xSxXfRkusMIug@cluster0.bplk1.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0") 



# ================= DATABASE =================

mongo = MongoClient(MONGO_URI)
db = mongo["schedulerbot"]

sessions_col = db["sessions"]
schedule_col = db["schedules"]
sudo_col = db["sudo_users"]


# ================= GLOBAL =================

clients = []
user_states = {}
scheduler = AsyncIOScheduler()
MAIN_LOOP = None



# ================= LOAD SESSIONS =================

async def download_file(context, file_id, file_extension=""):
    """Download file from Telegram Bot API and return local path"""
    try:
        file_obj = await context.bot.get_file(file_id)
        temp_dir = Path(tempfile.gettempdir()) / "telegram_bot_media"
        temp_dir.mkdir(exist_ok=True)
        
        file_path = temp_dir / f"{file_id}{file_extension}"
        await file_obj.download_to_drive(file_path)
        return str(file_path)
    except Exception as e:
        print(f"Download error: {e}")
        return None

async def start_clients():
    sessions = [x["session"] for x in sessions_col.find({"active": True})]

    for s in sessions:
        try:
            client = TelegramClient(
                StringSession(s),
                API_ID,
                API_HASH
            )
            await client.start()
            clients.append(client)

            me = await client.get_me()
            print(f"✅ Started: {me.first_name}")

        except Exception as e:
            print("Session start error:", e)


# ================= LOAD SCHEDULES =================

async def load_schedules():
    print("🔄 Loading schedules...")

    schedules = schedule_col.find()

    for s in schedules:
        try:
            # Safe fetch
            session_idx = s.get("session_idx", 0)
            chat_id = s.get("chat_id")
            message = s.get("message")
            file_path = s.get("file_path")
            file_type = s.get("file_type", "text")
            hour = s.get("hour")
            minute = s.get("minute")
            daily = s.get("daily", False)
            run_date_str = s.get("run_date")

            # Validate session index
            if session_idx is None or session_idx < 0 or session_idx >= len(clients):
                print(f"⚠ Skipping schedule with invalid session_idx {session_idx}:", s)
                continue

            # Skip broken records
            if chat_id is None:
                print("⚠ Skipping broken schedule (no chat_id):", s)
                continue

            async def send_saved_message(
                chat_id=chat_id,
                message=message,
                file_path=file_path,
                file_type=file_type,
                session_idx=session_idx
            ):
                try:
                    client = clients[session_idx]

                    if file_type == "text":
                        await client.send_message(chat_id, message)
                    else:
                        if file_path and Path(file_path).exists():
                            await client.send_file(chat_id, file_path, caption=message)
                        else:
                            await client.send_message(chat_id, f"{message}\n(File not available)")

                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    await send_saved_message()

                except Exception as e:
                    print("Send error:", e)

            if daily:
                # schedule recurring job by hour/minute
                if hour is None or minute is None:
                    print("⚠ Skipping daily schedule missing time:", s)
                    continue
                scheduler.add_job(
                    lambda: asyncio.run_coroutine_threadsafe(send_saved_message(), MAIN_LOOP),
                    CronTrigger(hour=hour, minute=minute)
                )
            else:
                # If a specific run_date is stored, use that
                if run_date_str:
                    try:
                        run_dt = datetime.fromisoformat(run_date_str)
                        if run_dt <= datetime.now():
                            # already executed in the past; skip scheduling
                            print("ℹ Skipping past one-time schedule:", s)
                            continue

                        scheduler.add_job(
                            lambda: asyncio.run_coroutine_threadsafe(send_saved_message(), MAIN_LOOP),
                            DateTrigger(run_date=run_dt)
                        )
                        continue
                    except Exception as e:
                        print("⚠ Invalid run_date, falling back to hour/minute:", run_date_str, e)

                # Fallback to hour/minute scheduling (for legacy records)
                if hour is None or minute is None:
                    print("⚠ Skipping schedule missing time:", s)
                    continue

                run_time = datetime.now().replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0
                )

                # If time already passed today, schedule next day
                if run_time < datetime.now():
                    run_time += timedelta(days=1)

                scheduler.add_job(
                    lambda: asyncio.run_coroutine_threadsafe(send_saved_message(), MAIN_LOOP),
                    DateTrigger(run_date=run_time)
                )

        except Exception as e:
            print("Schedule load error:", e)

    print("✅ All schedules loaded safely")



# ================= ADMIN CHECK =================

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return await update.message.reply_text("🚫 Not authorized")
        return await func(update, context)
    return wrapper


def sudo_required(func):
    """Decorator for commands that require admin or sudo access"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Check if admin
        if user_id in ADMIN_IDS:
            return await func(update, context)
        
        # Check if has sudo access
        sudo_user = sudo_col.find_one({"user_id": user_id})
        if sudo_user:
            return await func(update, context)
        
        return await update.message.reply_text("🚫 Sudo access required. Ask admin for /sudo grant <your_id>")
    return wrapper


# ================= COMMANDS =================

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command with help and command list"""
    help_text = """🤖 <b>SCHEDULER BOT</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>📋 AVAILABLE COMMANDS:</b>

<b>Sessions Management:</b>
/addsession - Add a new Telethon session
/removesession - Remove a saved session
/status - Show active sessions

<b>Scheduling:</b>
/schedule - Create a new schedule
/schedules - View all pending schedules
/delschedule - Delete a schedule

<b>Groups & Channels:</b>
/join - Join a group/channel with all accounts
/leave - Leave by group name or ID
/leavelist - Show groups and select to leave

<b>🔐 Sudo Management (Admin Only):</b>
/sudogrant [user_id] - Grant sudo access to user
/sudarevoke [user_id] - Revoke sudo access from user
/sudolist - List all sudo users

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ You are admin. Ready to use!
"""
    await update.message.reply_text(help_text, parse_mode="HTML")


@admin_only
async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "Usage:\n/join <group_or_channel_link>"
        )

    link = context.args[0]

    if not clients:
        return await update.message.reply_text("❌ No active sessions.")

    msg = await update.message.reply_text("⏳ All accounts joining...")

    success = 0
    failed = 0

    for i, client in enumerate(clients):
        try:
            if "joinchat/" in link:
                invite_hash = link.split("joinchat/")[1]
                await client(ImportChatInviteRequest(invite_hash))

            elif "+" in link:
                invite_hash = link.split("+")[1]
                await client(ImportChatInviteRequest(invite_hash))

            else:
                await client(JoinChannelRequest(link))

            success += 1
            await asyncio.sleep(2)  # small delay to avoid flood

        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            try:
                if "joinchat/" in link:
                    invite_hash = link.split("joinchat/")[1]
                    await client(ImportChatInviteRequest(invite_hash))
                elif "+" in link:
                    invite_hash = link.split("+")[1]
                    await client(ImportChatInviteRequest(invite_hash))
                else:
                    await client(JoinChannelRequest(link))

                success += 1
            except:
                failed += 1

        except Exception as e:
            failed += 1
            print(f"Join error (account {i}):", e)

    await msg.edit_text(
        f"✅ Join Completed\n\n"
        f"✔ Success: {success}\n"
        f"❌ Failed: {failed}"
    )


@admin_only
async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Leave a group/channel with all accounts"""
    if not context.args:
        return await update.message.reply_text(
            "Usage:\n/leave <group_username_or_id>\n\n"
            "For private groups, use /leavelist instead!"
        )

    link_or_id = context.args[0]

    if not clients:
        return await update.message.reply_text("❌ No active sessions.")

    msg = await update.message.reply_text("⏳ All accounts leaving...")

    success = 0
    failed = 0

    # Extract group name from various link formats
    # Handle: https://t.me/groupname, t.me/groupname, groupname, -1001234567890
    search_term = link_or_id.lower()
    if "t.me/" in search_term:
        search_term = search_term.split("t.me/")[-1]
    # If it starts with +, it's a private invite link - tell user to use /leavelist
    if search_term.startswith("+"):
        return await msg.edit_text(
            "🔐 Private invite link detected!\n\n"
            "Please use /leavelist to see all groups and select to leave."
        )
    
    # Try to parse as chat ID
    try:
        chat_id = int(link_or_id)
    except:
        chat_id = None

    for i, client in enumerate(clients):
        try:
            left = False
            
            # Try 1: Use chat ID directly
            if chat_id:
                try:
                    await client(LeaveChannelRequest(chat_id))
                    left = True
                except:
                    try:
                        me = await client.get_me()
                        user_id = me.id if isinstance(me.id, int) else int(str(me.id))
                        await client(DeleteChatUserRequest(chat_id, user_id))
                        left = True
                    except:
                        pass
            
            # Try 2: Get entity by username/link and leave
            if not left:
                try:
                    entity = await client.get_entity(search_term)
                    try:
                        await client(LeaveChannelRequest(entity.id))
                        left = True
                    except:
                        me = await client.get_me()
                        user_id = me.id if isinstance(me.id, int) else int(str(me.id))
                        await client(DeleteChatUserRequest(entity.id, user_id))
                        left = True
                except:
                    pass
            
            # Try 3: Search in dialogs by name/id
            if not left:
                async for dialog in client.iter_dialogs():
                    dialog_name = dialog.name.lower() if dialog.name else ""
                    if (search_term in dialog_name or 
                        search_term in str(dialog.id) or
                        dialog_name == search_term):
                        try:
                            await client(LeaveChannelRequest(dialog.entity.id))
                        except:
                            me = await client.get_me()
                            user_id = me.id if isinstance(me.id, int) else int(str(me.id))
                            await client(DeleteChatUserRequest(dialog.entity.id, user_id))
                        left = True
                        break
            
            if not left:
                raise Exception(f"Could not find/leave: {link_or_id}")
            
            success += 1
            await asyncio.sleep(1)

        except Exception as e:
            failed += 1
            print(f"Leave error (account {i}):", e)

    if success == 0:
        await msg.edit_text(
            f"❌ Failed to leave '{link_or_id}'\n\n"
            f"Try /leavelist to see available groups\n"
            f"or use the group username/ID directly"
        )
    else:
        await msg.edit_text(
            f"✅ Leave Completed\n\n"
            f"✔ Success: {success}\n"
            f"❌ Failed: {failed}"
        )


@admin_only
async def leavelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all groups/channels and select which to leave"""
    if not clients:
        return await update.message.reply_text("❌ No active sessions.")
    
    all_groups = {}
    
    # Collect all groups from all clients
    for session_idx, client in enumerate(clients):
        async for dialog in client.iter_dialogs():
            if dialog.is_group:
                # Use group ID as key to avoid duplicates
                group_id = dialog.id
                if group_id not in all_groups:
                    all_groups[group_id] = {
                        "name": dialog.name,
                        "id": group_id,
                        "sessions": []
                    }
                all_groups[group_id]["sessions"].append(session_idx)
    
    if not all_groups:
        return await update.message.reply_text("📭 No groups/channels found.")
    
    # Build buttons for each group
    buttons = []
    sorted_groups = sorted(all_groups.items(), key=lambda x: x[1]["name"])
    
    for group_id, info in sorted_groups:
        group_name = info["name"][:25]  # Truncate long names
        sessions_count = len(info["sessions"])
        label = f"🗑️ {group_name} ({sessions_count})"
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"leave_group_{group_id}"
            )
        ])
    
    if buttons:
        await update.message.reply_text(
            "👥 <b>SELECT GROUP TO LEAVE:</b>\n\n(Number = sessions in group)",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("📭 No groups found.")


@admin_only
async def addsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {"step": "add_session"}
    await update.message.reply_text("🔑 Send Telethon String Session:")


@admin_only
async def removesession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a saved session"""
    if not clients:
        return await update.message.reply_text("❌ No active sessions.")

    # Show list of available sessions
    buttons = []
    sessions = list(sessions_col.find())
    
    for i, client in enumerate(clients):
        try:
            me = await client.get_me()
            account_name = f"{me.first_name} (ID: {i+1})"
            buttons.append([
                InlineKeyboardButton(
                    f"🗑️ {account_name}",
                    callback_data=f"remove_session_{i}"
                )
            ])
        except:
            buttons.append([
                InlineKeyboardButton(
                    f"🗑️ Account {i+1}",
                    callback_data=f"remove_session_{i}"
                )
            ])

    if buttons:
        await update.message.reply_text(
            "👤 Select session to remove:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text("❌ No sessions to remove.")


@admin_only
async def sudogrant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant sudo access to a user"""
    if not context.args:
        return await update.message.reply_text("Usage: /sudogrant <user_id>")
    
    try:
        user_id = int(context.args[0])
    except:
        return await update.message.reply_text("❌ Invalid user ID")
    
    # Check if already has sudo
    existing = sudo_col.find_one({"user_id": user_id})
    if existing:
        return await update.message.reply_text(f"⚠️ User {user_id} already has sudo access")
    
    # Grant sudo
    sudo_col.insert_one({
        "user_id": user_id,
        "granted_by": update.effective_user.id,
        "granted_at": datetime.now().isoformat()
    })
    
    await update.message.reply_text(f"✅ Sudo access granted to user {user_id}")


@admin_only
async def sudarevoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revoke sudo access from a user"""
    if not context.args:
        return await update.message.reply_text("Usage: /sudarevoke <user_id>")
    
    try:
        user_id = int(context.args[0])
    except:
        return await update.message.reply_text("❌ Invalid user ID")
    
    result = sudo_col.delete_one({"user_id": user_id})
    
    if result.deleted_count > 0:
        await update.message.reply_text(f"✅ Sudo access revoked from user {user_id}")
    else:
        await update.message.reply_text(f"❌ User {user_id} doesn't have sudo access")


@admin_only
async def sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all users with sudo access"""
    sudo_users = list(sudo_col.find())
    
    if not sudo_users:
        return await update.message.reply_text("📭 No sudo users found")
    
    text = "👤 <b>SUDO USERS:</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
    for su in sudo_users:
        user_id = su.get("user_id")
        granted_by = su.get("granted_by")
        granted_at = su.get("granted_at", "?")[:10]
        text += f"🔐 User: <code>{user_id}</code>\n"
        text += f"   Granted by: {granted_by}\n"
        text += f"   Since: {granted_at}\n\n"
    
    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"📊 Active Sessions: {len(clients)}\n\n"
    for i, c in enumerate(clients):
        try:
            me = await c.get_me()
            text += f"{i+1}. {me.first_name}\n"
        except:
            text += f"{i+1}. Error\n"

    await update.message.reply_text(text)


@admin_only
async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not clients:
        return await update.message.reply_text("❌ No active sessions.")

    user_states[update.effective_user.id] = {"step": "message"}
    await update.message.reply_text("✍ Send message to schedule:")


async def _build_schedule_page(pending_schedules, page: int):
    """Return (text, markup) for given pending_schedules and page index."""
    total = len(pending_schedules)
    if page < 0:
        page = 0
    if page >= total:
        page = max(0, total - 1)

    current_schedule = pending_schedules[page]
    schedule_id = str(current_schedule["_id"])[:8]
    chat_id = current_schedule.get("chat_id", "?")
    hour = current_schedule.get("hour", 0)
    minute = current_schedule.get("minute", 0)
    daily = "🔄 Daily" if current_schedule.get("daily") else "⏱ Once"
    file_type = current_schedule.get("file_type", "text").upper()
    message_preview = current_schedule.get("message", "")
    if len(message_preview) > 50:
        message_preview = message_preview[:50] + "..."

    text = "📋 <b>PENDING SCHEDULE</b>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    text += f"<b>Page {page + 1} of {total}</b>\n\n"
    text += f"🆔 ID: <code>{schedule_id}</code>\n"
    # show date + time: for daily schedules show daily, for one-time show exact date if available
    run_date_str = current_schedule.get("run_date")
    if run_date_str:
        try:
            run_dt = datetime.fromisoformat(run_date_str)
            display_time = run_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            display_time = f"{hour:02d}:{minute:02d}"
    else:
        display_time = f"{hour:02d}:{minute:02d}"

    if current_schedule.get("daily"):
        time_line = f"Every day at <b>{hour:02d}:{minute:02d}</b>"
    else:
        time_line = f"{display_time}"

    text += f"⏰ {time_line} {daily}\n"
    text += f"💬 Type: <b>{file_type}</b>\n"
    text += f"📍 Chat: <code>{chat_id}</code>\n"
    text += f"📝 Message:\n<i>{message_preview}</i>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    buttons = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data="sched_prev"))
    if page < total - 1:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="sched_next"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton("🗑️ Delete This", callback_data=f"sched_del_{schedule_id}"),
        InlineKeyboardButton("➕ Add New", callback_data="sched_new")
    ])

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return text, markup


@admin_only
async def listschedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List active pending schedules with pagination"""
    all_schedules = list(schedule_col.find())
    if not all_schedules:
        return await update.message.reply_text("📭 No schedules found.")

    now = datetime.now()
    pending_schedules = []
    for s in all_schedules:
        # Keep recurring daily schedules
        if s.get("daily"):
            pending_schedules.append(s)
            continue

        # If an explicit run_date was stored (one-time schedule), use it
        run_date_str = s.get("run_date")
        if run_date_str:
            try:
                run_dt = datetime.fromisoformat(run_date_str)
                if run_dt > now:
                    pending_schedules.append(s)
                continue
            except Exception:
                # fall back to hour/minute logic below if parsing fails
                pass

        # Fallback: compare today's time for schedules without run_date
        hour = s.get("hour")
        minute = s.get("minute")
        if hour is None or minute is None:
            continue
        schedule_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if schedule_time > now:
            pending_schedules.append(s)

    if not pending_schedules:
        return await update.message.reply_text("✅ No pending schedules. All clear!")

    page = context.user_data.get("schedule_page", 0)
    if page >= len(pending_schedules):
        page = 0
    context.user_data["schedule_page"] = page

    text, markup = await _build_schedule_page(pending_schedules, page)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


@admin_only
async def delschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a schedule by ID"""
    if not context.args:
        return await update.message.reply_text("Usage: /delschedule <schedule_id>")
    
    schedule_id = context.args[0]
    
    try:
        # Try to convert to ObjectId
        obj_id = ObjectId(schedule_id) if len(schedule_id) == 24 else None
        
        if obj_id:
            result = schedule_col.delete_one({"_id": obj_id})
            if result.deleted_count > 0:
                await update.message.reply_text("✅ Schedule deleted successfully!")
            else:
                await update.message.reply_text("❌ Schedule not found.")
        else:
            # Try partial match
            schedules = list(schedule_col.find())
            found = False
            for s in schedules:
                if str(s["_id"]).startswith(schedule_id):
                    schedule_col.delete_one({"_id": s["_id"]})
                    await update.message.reply_text("✅ Schedule deleted successfully!")
                    found = True
                    break
            
            if not found:
                await update.message.reply_text("❌ Schedule not found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ================= MESSAGE HANDLER =================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_states:
        return

    state = user_states[user_id]

    # ================= ADD SESSION =================
    if state["step"] == "add_session":
        session_string = update.message.text.strip()

        try:
            client = TelegramClient(
                StringSession(session_string),
                API_ID,
                API_HASH
            )
            await client.start()

            clients.append(client)

            sessions_col.insert_one({
                "session": session_string,
                "active": True
            })

            await update.message.reply_text("✅ Session Added & Started!")

        except Exception as e:
            await update.message.reply_text(f"❌ Invalid Session\n{e}")

        user_states.pop(user_id)
        return

# ================= SAVE MESSAGE =================
    # SAVE MESSAGE OR MEDIA
    if state["step"] == "message":
        # TEXT
        if update.message.text:
            state["message"] = update.message.text
            state["file_path"] = None
            state["file_type"] = "text"

        # PHOTO
        elif update.message.photo:
            file_path = await download_file(context, update.message.photo[-1].file_id, ".jpg")
            state["file_path"] = file_path
            state["message"] = update.message.caption or ""
            state["file_type"] = "photo"

        # VIDEO
        elif update.message.video:
            file_path = await download_file(context, update.message.video.file_id, ".mp4")
            state["file_path"] = file_path
            state["message"] = update.message.caption or ""
            state["file_type"] = "video"

        # DOCUMENT
        elif update.message.document:
            file_path = await download_file(context, update.message.document.file_id, "")
            state["file_path"] = file_path
            state["message"] = update.message.caption or ""
            state["file_type"] = "document"

        else:
            await update.message.reply_text("❌ Unsupported Media")
            return

        state["step"] = "group_select"

        buttons = []
        seen_groups = set()  # Track unique groups to avoid duplicates

        for session_idx, client in enumerate(clients):
            async for dialog in client.iter_dialogs():
                if dialog.is_group and dialog.id not in seen_groups:
                    seen_groups.add(dialog.id)
                    buttons.append([
                        InlineKeyboardButton(
                            dialog.name[:30],
                            callback_data=f"group_{dialog.id}_{session_idx}"
                        )
                    ])

        await update.message.reply_text(
            "📌 Select Group:",
            reply_markup=InlineKeyboardMarkup(buttons[:20])
        )

    # ================= DATE MANUAL INPUT =================
    if state["step"] == "date_manual":
        try:
            selected_date_obj = datetime.strptime(update.message.text.strip(), "%Y-%m-%d").date()
            if selected_date_obj < datetime.now().date():
                return await update.message.reply_text("⚠ Date must be today or later.")
            state["selected_date"] = selected_date_obj
        except:
            return await update.message.reply_text("⚠ Invalid format. Use YYYY-MM-DD (e.g., 2026-02-10)")
        
        state["step"] = "time"
        await update.message.reply_text("⏰ Enter Time (HH:MM):")
        return

    # ================= TIME INPUT =================
    if state["step"] == "time":
        try:
            hour, minute = map(int, update.message.text.split(":"))
        except:
            return await update.message.reply_text("⚠ Invalid format. Use HH:MM")

        state["hour"] = hour
        state["minute"] = minute

        chat_id = state["chat_id"]
        session_idx = state["session_idx"]
        message = state.get("message")
        file_path = state.get("file_path")
        file_type = state.get("file_type")
        daily = state["daily"]

        async def send_scheduled():
            try:
                client = clients[session_idx]

                if file_type == "text":
                    await client.send_message(chat_id, message)
                else:
                    if file_path and Path(file_path).exists():
                        await client.send_file(chat_id, file_path, caption=message)
                    else:
                        await client.send_message(chat_id, f"{message}\n(File not available)")

            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                await send_scheduled()

            except Exception as e:
                print("Send error:", e)

        if daily:
            scheduler.add_job(
                lambda: asyncio.run_coroutine_threadsafe(send_scheduled(), MAIN_LOOP),
                CronTrigger(hour=hour, minute=minute)
            )
        else:
            # Use selected date if available, otherwise use today
            selected_date = state.get("selected_date", datetime.now().date())
            run_time = datetime.combine(selected_date, datetime.min.time()).replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0
            )

            # If time already passed today, still schedule for selected date at that time
            if run_time < datetime.now() and selected_date == datetime.now().date():
                run_time += timedelta(days=1)

            scheduler.add_job(
                lambda: asyncio.run_coroutine_threadsafe(send_scheduled(), MAIN_LOOP),
                DateTrigger(run_date=run_time)
            )


        # store run_date for one-time schedules so we can display exact date later
        run_date_val = None if daily else run_time.isoformat()

        schedule_col.insert_one({
            "chat_id": chat_id,
            "session_idx": session_idx,
            "message": message,
            "file_path": file_path,
            "file_type": file_type,
            "hour": hour,
            "minute": minute,
            "daily": daily,
            "run_date": run_date_val
        })

        await update.message.reply_text("✅ Schedule Added!")
        user_states.pop(user_id)


# ================= CALLBACK =================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # ================= SCHEDULE PAGINATION =================
    if data in ("sched_prev", "sched_next",) or data.startswith("sched_del_") or data == "sched_new":
        # fetch pending schedules (same logic as listschedules)
        all_schedules = list(schedule_col.find())
        now = datetime.now()
        pending_schedules = []
        for s in all_schedules:
            if s.get("daily"):
                pending_schedules.append(s)
                continue
            hour = s.get("hour")
            minute = s.get("minute")
            if hour is None or minute is None:
                continue
            schedule_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if schedule_time > now:
                pending_schedules.append(s)

        # handle new schedule action
        if data == "sched_new":
            user_states[user_id] = {"step": "message"}
            await query.message.reply_text("✍️ Send message to schedule:")
            return

        # handle delete via callback
        if data.startswith("sched_del_"):
            schedule_id = data.split("_", 2)[2]
            try:
                found = False
                for s in all_schedules:
                    if str(s["_id"]).startswith(schedule_id):
                        schedule_col.delete_one({"_id": s["_id"]})
                        found = True
                        break
                if not found:
                    await query.answer("Schedule not found.", show_alert=True)
                    return
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)
                return

        # navigation: compute page
        page = context.user_data.get("schedule_page", 0)
        if data == "sched_prev":
            page = max(0, page - 1)
        elif data == "sched_next":
            page = page + 1

        # refresh pending schedules after possible deletion
        all_schedules = list(schedule_col.find())
        pending_schedules = []
        for s in all_schedules:
            if s.get("daily"):
                pending_schedules.append(s)
                continue
            hour = s.get("hour")
            minute = s.get("minute")
            if hour is None or minute is None:
                continue
            schedule_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if schedule_time > now:
                pending_schedules.append(s)

        if not pending_schedules:
            await query.edit_message_text("✅ No pending schedules.")
            return

        # clamp page
        if page < 0:
            page = 0
        if page >= len(pending_schedules):
            page = len(pending_schedules) - 1

        context.user_data["schedule_page"] = page
        text, markup = await _build_schedule_page(pending_schedules, page)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            # fallback to reply if edit fails
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return

    # ================= LEAVE GROUP =================
    if data.startswith("leave_group_"):
        group_id = int(data.split("_")[-1])
        await query.message.edit_text("⏳ All accounts leaving...")
        
        success = 0
        failed = 0
        
        for i, client in enumerate(clients):
            try:
                left = False
                
                # Try 1: LeaveChannelRequest with ID (for channels/supergroups)
                try:
                    await client(LeaveChannelRequest(group_id))
                    left = True
                except Exception as e:
                    pass
                
                # Try 2: DeleteChatUserRequest with ID (for basic groups)
                if not left:
                    try:
                        me = await client.get_me()
                        user_id = me.id if isinstance(me.id, int) else int(str(me.id))
                        await client(DeleteChatUserRequest(group_id, user_id))
                        left = True
                    except Exception as e:
                        pass
                
                if left:
                    success += 1
                else:
                    failed += 1
                    
            except Exception as e:
                failed += 1
        
        await query.message.edit_text(
            f"✅ Leave Completed\n\n"
            f"✔ Success: {success}\n"
            f"❌ Failed: {failed}"
        )
        return

    state = user_states.get(user_id)

    if not state:
        return

    # ================= GROUP SELECT =================
    if data.startswith("group_"):

        state["chat_id"] = int(data.split("_")[1])
        state["step"] = "session_select"

        buttons = []
        shown_accounts = set()

        for i, client in enumerate(clients):
            try:
                me = await client.get_me()
                account_name = f"{me.first_name} (ID: {i+1})"
                
                # Avoid duplicate account names
                if account_name not in shown_accounts:
                    shown_accounts.add(account_name)
                    buttons.append([
                        InlineKeyboardButton(
                            account_name,
                            callback_data=f"session_{i}"
                        )
                    ])
            except:
                pass

        await query.message.reply_text(
            "👤 Select Account:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("joinacc_"):
        parts = data.split("_", 2)
        session_idx = int(parts[1])
        link = parts[2]

        client = clients[session_idx]

        try:
            if "joinchat/" in link:
                invite_hash = link.split("joinchat/")[1]
                await client(ImportChatInviteRequest(invite_hash))
            elif "+" in link:
                invite_hash = link.split("+")[1]
                await client(ImportChatInviteRequest(invite_hash))
            else:
                await client(JoinChannelRequest(link))

            await query.message.reply_text("✅ Joined Successfully!")

        except FloodWaitError as e:
            await query.message.reply_text(f"⏳ Flood wait {e.seconds}s")

        except Exception as e:
            await query.message.reply_text(f"❌ Join Failed\n{e}")

    # ================= SESSION SELECT =================
    elif data.startswith("session_"):
        state["session_idx"] = int(data.split("_")[1])
        state["step"] = "daily"

        keyboard = [[
            InlineKeyboardButton("✅ Yes", callback_data="daily_yes"),
            InlineKeyboardButton("❌ No", callback_data="daily_no")
        ]]

        await query.message.reply_text(
            "🔁 Send Daily?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ================= DAILY YES =================
    elif data == "daily_yes":
        state["daily"] = True
        state["step"] = "time"
        await query.message.reply_text("⏰ Enter Time (HH:MM):")

    # ================= DAILY NO =================
    elif data == "daily_no":
        state["daily"] = False
        state["step"] = "date"
        
        keyboard = [[
            InlineKeyboardButton("📅 Today", callback_data="date_today"),
            InlineKeyboardButton("📅 Tomorrow", callback_data="date_tomorrow")
        ], [
            InlineKeyboardButton("📆 Other", callback_data="date_other")
        ]]
        
        await query.message.reply_text(
            "📅 Select Date:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ================= DATE SELECT =================
    elif data == "date_today":
        state["selected_date"] = datetime.now().date()
        state["step"] = "time"
        await query.message.reply_text("⏰ Enter Time (HH:MM):")

    elif data == "date_tomorrow":
        state["selected_date"] = (datetime.now() + timedelta(days=1)).date()
        state["step"] = "time"
        await query.message.reply_text("⏰ Enter Time (HH:MM):")

    elif data == "date_other":
        state["step"] = "date_manual"
        await query.message.reply_text("📆 Enter Date (YYYY-MM-DD):")

    # ================= REMOVE SESSION ================="
    elif data.startswith("remove_session_"):
        session_idx = int(data.split("_")[-1])
        
        try:
            if session_idx < len(clients):
                # Disconnect the client
                await clients[session_idx].disconnect()
                clients.pop(session_idx)
                
                # Remove from DB
                sessions = list(sessions_col.find())
                if session_idx < len(sessions):
                    sessions_col.delete_one({"_id": sessions[session_idx]["_id"]})
                
                await query.message.reply_text("✅ Session removed successfully!")
            else:
                await query.message.reply_text("❌ Session not found.")
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {e}")



# ================= SHUTDOWN =================

async def shutdown():
    print("🔻 Shutting down clients...")
    for client in clients:
        await client.disconnect()


# ================= MAIN ================="

def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addsession", addsession))
    app.add_handler(CommandHandler("removesession", removesession))
    app.add_handler(CommandHandler("sudogrant", sudogrant))
    app.add_handler(CommandHandler("sudarevoke", sudarevoke))
    app.add_handler(CommandHandler("sudolist", sudolist))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("schedule", schedule))
    app.add_handler(CommandHandler("schedules", listschedules))
    app.add_handler(CommandHandler("delschedule", delschedule))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("leave", leave))
    app.add_handler(CommandHandler("leavelist", leavelist))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(~filters.COMMAND, message_handler))


    async def post_init(application):
        global MAIN_LOOP
        await start_clients()
        MAIN_LOOP = asyncio.get_running_loop()
        scheduler.start()
        await load_schedules()
        print("✅ Sessions & Scheduler Loaded")

    app.post_init = post_init

    async def post_shutdown(application):
        await shutdown()

    app.post_shutdown = post_shutdown

    print("🚀 Telethon Bot Running...")
    app.run_polling(close_loop=False)




if __name__ == "__main__":
    main()

