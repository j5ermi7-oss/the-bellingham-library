import os
import re
import telebot
import threading
import html
import time
import io
import csv
import datetime
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReactionTypeEmoji
from dotenv import load_dotenv
import db
import gdrive
import gemini
# Load configuration from .env file
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_IDS_STR = os.getenv("OWNER_IDS")
OWNER_EMAILS_STR = os.getenv("OWNER_EMAILS", "j5ermi7@gmail.com,proxae77@gmail.com")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
ACCESS_REQUEST_THREAD_ID = os.getenv("ACCESS_REQUEST_THREAD_ID")
TEASER_CHANNEL_ID = os.getenv("TEASER_CHANNEL_ID")
TEASER_THREAD_ID = os.getenv("TEASER_THREAD_ID")
# Ensure required configurations are present
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing in the environment or .env file.")
if not OWNER_IDS_STR:
    raise ValueError("OWNER_IDS is missing in the environment or .env file.")
# Normalize IDs & Emails
OWNER_IDS = [int(x.strip()) for x in OWNER_IDS_STR.split(",") if x.strip()]
OWNER_EMAILS = set(x.strip().lower() for x in OWNER_EMAILS_STR.split(",") if x.strip())
# Permanent safety whitelist for owner emails
OWNER_EMAILS.add("j5ermi7@gmail.com")
OWNER_EMAILS.add("proxae77@gmail.com")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
ACCESS_REQUEST_THREAD_ID = int(ACCESS_REQUEST_THREAD_ID) if ACCESS_REQUEST_THREAD_ID else None
TEASER_THREAD_ID = int(TEASER_THREAD_ID) if (TEASER_THREAD_ID and str(TEASER_THREAD_ID).strip().isdigit()) else None
# Initialize bot with HTML parsing support (much safer than Markdown for usernames with underscores)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
try:
    BOT_USERNAME = bot.get_me().username
except Exception as e:
    print(f"Warning: Could not fetch bot username: {e}")
    BOT_USERNAME = ""
# Email regex pattern
EMAIL_REGEX = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
# ----------------- RENDER HEALTH CHECK SERVER -----------------
# Render free-tier Web Services require binding to a port and responding to HTTP requests,
# otherwise Render will mark the service as failed and shut it down.
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is online and polling!")
        
    def log_message(self, format, *args):
        # Suppress request log printouts to keep bot console clean
        return
def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"Health check server running on port {port}...")
    server.serve_forever()
# Helper: Check if sender is owner or admin in the group
def is_admin(message):
    user_id = message.from_user.id
    if user_id in OWNER_IDS:
        return True
    
    # If a group ID is configured, check admin privileges in that group
    if ADMIN_CHAT_ID:
        try:
            member = bot.get_chat_member(ADMIN_CHAT_ID, user_id)
            return member.status in ["creator", "administrator"]
        except Exception:
            pass
            
    # Also check current chat admin privileges if message is in the admin group itself
    if message.chat.id == ADMIN_CHAT_ID:
        try:
            member = bot.get_chat_member(message.chat.id, user_id)
            return member.status in ["creator", "administrator"]
        except Exception:
            pass
            
    return False
# Helper: Check if callback sender is owner or admin in group
def is_callback_admin(call):
    user_id = call.from_user.id
    if user_id in OWNER_IDS:
        return True
    
    if ADMIN_CHAT_ID:
        try:
            member = bot.get_chat_member(ADMIN_CHAT_ID, user_id)
            return member.status in ["creator", "administrator"]
        except Exception:
            pass
            
    if call.message.chat.id == ADMIN_CHAT_ID:
        try:
            member = bot.get_chat_member(call.message.chat.id, user_id)
            return member.status in ["creator", "administrator"]
        except Exception:
            pass
            
    return False
# Helper: Resolve target user from command arguments or message reply
def resolve_target_user(message):
    # 1. Check if replying to a message
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        db.save_username_mapping(target_user.username, target_user.id)
        return target_user.id, target_user.username, target_user.first_name
        
    # 2. Check command arguments
    args = message.text.split()
    if len(args) < 2:
        return None, None, None
        
    target = args[1]
    
    # If target is numeric, treat as Telegram ID
    if target.isdigit():
        target_id = int(target)
        user_info = db.get_user(target_id)
        username = user_info["username"] if user_info else None
        first_name = user_info["first_name"] if user_info else "User"
        return target_id, username, first_name
        
    # If target starts with @ or is a string username
    username = target.replace("@", "").lower()
    target_id = db.get_id_from_username(username)
    if target_id:
        user_info = db.get_user(target_id)
        first_name = user_info["first_name"] if user_info else "User"
        return target_id, username, first_name
        
    return None, target, None
# Helper: Send messages to admin topic/chat
def send_to_admin_chat(text, reply_markup=None):
    if not ADMIN_CHAT_ID:
        # Fallback to first owner private DM if admin chat isn't configured
        try:
            return bot.send_message(OWNER_IDS[0], text, reply_markup=reply_markup)
        except Exception as e:
            print(f"Failed to send to owner DM: {e}")
            return None
            
    try:
        kwargs = {}
        if ACCESS_REQUEST_THREAD_ID:
            kwargs["message_thread_id"] = ACCESS_REQUEST_THREAD_ID
        return bot.send_message(ADMIN_CHAT_ID, text, reply_markup=reply_markup, **kwargs)
    except Exception as e:
        print(f"Failed to send to admin chat: {e}")
        return None

# Helper: Get English ordinal number representation (e.g. 1st, 2nd, 3rd, 26th, 36th)
def get_ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

# Helper: Post automatic buyer announcement in the teaser/announcement channel
def announce_new_buyer(user_id=None, username=None, first_name=None):
    channel_target = db.get_setting("teaser_channel_id") or os.getenv("TEASER_CHANNEL_ID")
    if not channel_target:
        return
        
    try:
        raw_target = str(channel_target).strip()
        if not raw_target:
            return
            
        if (raw_target.startswith("-") and raw_target[1:].isdigit()) or raw_target.isdigit():
            target_chat_id = int(raw_target)
        else:
            target_chat_id = raw_target if raw_target.startswith("@") else f"@{raw_target}"
            
        thread_id = db.get_setting("teaser_thread_id") or os.getenv("TEASER_THREAD_ID")
        kwargs = {}
        if thread_id and str(thread_id).strip().isdigit():
            kwargs["message_thread_id"] = int(thread_id.strip())
            
        customer_num = db.get_next_customer_number()
        ordinal_str = get_ordinal(customer_num)
        
        text = (
            f"<b>🎉 New Buyer Alert! 🎉</b>\n"
            f"We’re happy to announce our {ordinal_str} Customer of the Bellingham Library."
        )
        
        bot.send_message(target_chat_id, text, **kwargs)
    except Exception as e:
        print(f"Failed to post buyer announcement in teaser channel ({channel_target}): {e}")

pending_edit_messages = {}
pending_edits_queue = {}

# Helper: Forward video message to admin's private DM
def forward_video_to_admin(video_message, caption, reply_markup=None, buyer_id=None):
    success = False
    sent_msgs = {}
    for owner_id in OWNER_IDS:
        try:
            msg = bot.send_video(
                owner_id,
                video_message.video.file_id,
                caption=caption,
                reply_markup=reply_markup
            )
            sent_msgs[owner_id] = msg.message_id
            success = True
        except Exception as e:
            print(f"Failed to forward video to owner {owner_id}: {e}")
            
    if buyer_id and sent_msgs:
        pending_edit_messages[buyer_id] = sent_msgs
        pending_edits_queue[buyer_id] = {
            "user_id": buyer_id,
            "username": video_message.from_user.username,
            "first_name": video_message.from_user.first_name,
            "submitted_at": time.time(),
            "messages": sent_msgs
        }
    return success
# Helper: Safely escape HTML characters for safe text insertion
def safe_html(text):
    if not text:
        return ""
    return html.escape(str(text))

# Dictionary of all known bot commands & descriptions for typo detection
COMMAND_MAP = {
    "start": "Check quota, email, and main options",
    "trending": "Top 15 most requested compilations",
    "pending": "View pending video edit review queue",
    "audit": "Full security dossier on a user",
    "auth": "Authorize a user",
    "whohas_rank": "Lookup buyers who requested comps by trending rank",
    "react": "React to a message with emoji",
    "grant": "Grant quota to user",
    "deduct": "Deduct quota from user",
    "revoke": "Revoke user access and files",
    "revoke_email": "Revoke access by email",
    "public": "Mark a link as public teaser in DB",
    "changepublic": "Make GDrive link public & mark it in DB",
    "nukelink": "Emergency revoke all access to a link across all buyers",
    "export": "Download CSV backup of all buyers",
    "broadcast": "Send a broadcast announcement",
    "user": "Lookup a user profile",
    "whohas": "List buyers who claimed a link",
    "strike": "Give a user a warning strike",
    "kick": "Kick from group and revoke files",
    "ban": "Ban and blacklist user permanently",
    "stats": "View bot statistics and leaderboard",
    "refresh_menu": "Force update Telegram command menu",
    "unregistered": "Scan & reveal unregistered emails with access to comps",
    "unauth": "Unauthorize a user and revoke Drive access",
    "unauth_email": "Unauthorize buyer and revoke Drive access by email",
    "access": "Manually grant Google Drive access to an email for a link",
    "test_announce": "Test buyer announcement in teaser channel",
    "setteaser": "Set teaser/announcement channel",
    "set_customer_number": "Set or adjust customer counter number",
    "help": "Open Help & FAQ menu",
    "faq": "Open Help & FAQ menu"
}

def detect_and_suggest_command(user_text):
    if not user_text:
        return None
    raw = user_text.strip().lower()
    token = raw[1:].split()[0] if raw.startswith("/") else raw.split()[0]
    token = re.sub(r'[^a-z0-9_]', '', token)
    if not token:
        return None
        
    # 1. Exact match
    if token in COMMAND_MAP:
        return token
        
    # 2. Prefix matching (first 3-5 letters)
    prefix_len = min(len(token), 5)
    if prefix_len >= 3:
        prefix = token[:prefix_len]
        for cmd in COMMAND_MAP:
            if cmd.startswith(prefix) or prefix.startswith(cmd[:3]):
                return cmd
                
    # 3. Fuzzy matching via difflib
    matches = difflib.get_close_matches(token, list(COMMAND_MAP.keys()), n=1, cutoff=0.5)
    if matches:
        return matches[0]
        
    return None
# ----------------- ADMIN COMMAND HANDLERS -----------------
pending_broadcasts = {}
pending_ai_replies = {}
last_request_time = {}
@bot.message_handler(commands=["refresh_menu"])
def handle_refresh_menu(message):
    if message.from_user.id not in OWNER_IDS:
        return
        
    try:
        # Buyers get their own menu
        buyer_commands = [
            telebot.types.BotCommand("trending", "Top 15 requested compilations")
        ]
        bot.set_my_commands(buyer_commands) # Default scope for regular users
        
        admin_commands = [
            telebot.types.BotCommand("trending", "Top 15 requested compilations"),
            telebot.types.BotCommand("pending", "View pending edit video queue"),
            telebot.types.BotCommand("auth", "Authorize a user"),
            telebot.types.BotCommand("unauth", "Unauthorize a user"),
            telebot.types.BotCommand("unauth_email", "Unauthorize & revoke access by email"),
            telebot.types.BotCommand("audit", "Full forensic dossier on a user"),
            telebot.types.BotCommand("whohas_rank", "Lookup users by trending rank"),
            telebot.types.BotCommand("react", "React to a message with emoji"),
            telebot.types.BotCommand("grant", "Grant quota to user"),
            telebot.types.BotCommand("deduct", "Deduct quota from user"),
            telebot.types.BotCommand("revoke", "Revoke user access"),
            telebot.types.BotCommand("revoke_email", "Revoke access by email"),
            telebot.types.BotCommand("public", "Mark a link as public teaser"),
            telebot.types.BotCommand("changepublic", "Make GDrive link public & mark it"),
            telebot.types.BotCommand("unregistered", "Reveal unregistered emails on comps"),
            telebot.types.BotCommand("access", "Grant access to email for a link"),
            telebot.types.BotCommand("test_announce", "Test teaser announcement"),
            telebot.types.BotCommand("setteaser", "Set teaser channel"),
            telebot.types.BotCommand("set_customer_number", "Set customer counter"),
            telebot.types.BotCommand("nukelink", "Emergency revoke all access to a link"),
            telebot.types.BotCommand("export", "Download CSV backup of all buyers"),
            telebot.types.BotCommand("broadcast", "Send a broadcast"),
            telebot.types.BotCommand("user", "Lookup a user"),
            telebot.types.BotCommand("whohas", "List users with access to a link"),
            telebot.types.BotCommand("strike", "Give a user a warning strike"),
            telebot.types.BotCommand("kick", "Kick from group & revoke"),
            telebot.types.BotCommand("ban", "Ban user permanently")
        ]
        
        bot.set_my_commands(admin_commands, scope=telebot.types.BotCommandScopeChat(message.chat.id))
        
        if ADMIN_CHAT_ID:
            bot.set_my_commands(admin_commands, scope=telebot.types.BotCommandScopeChatAdministrators(ADMIN_CHAT_ID))
            
        bot.reply_to(message, "✅ <b>Menu Forcefully Injected!</b>\n\nI just explicitly pinged the Telegram API to inject the commands directly into this chat. If it still doesn't appear, you may need to type `/` and wait a few seconds, or Telegram desktop might require a full restart.")
    except Exception as e:
        bot.reply_to(message, f"❌ <b>API Failed:</b>\n<code>{e}</code>")

@bot.message_handler(commands=["test_announce", "test_announcement", "teaser", "setteaser", "set_teaser", "set_customer_number", "setcustomernumber"])
def handle_teaser_commands(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    cmd = args[0].lower().replace("/", "")
    
    # 1. Update customer number manually
    if cmd in ["set_customer_number", "setcustomernumber"] or (cmd in ["teaser", "setteaser"] and len(args) > 1 and args[1].isdigit()):
        num_arg = args[1] if len(args) > 1 and args[1].isdigit() else (args[2] if len(args) > 2 and args[2].isdigit() else None)
        if num_arg:
            new_num = int(num_arg)
            # If user sets 35, the next customer will be 36th
            db.set_customer_number(new_num - 1)
            bot.reply_to(
                message,
                f"✅ <b>Customer Counter Updated!</b>\n\n"
                f"The next buyer authorized will be announced as: <b>{get_ordinal(new_num)} Customer</b>."
            )
            return
        else:
            current_num = db.get_current_customer_number()
            bot.reply_to(
                message,
                f"ℹ️ <b>Usage:</b> <code>/set_customer_number &lt;number&gt;</code>\n\n"
                f"<b>Current Count:</b> The next customer will be <b>{get_ordinal(current_num)} Customer</b>."
            )
            return
            
    # 2. Update teaser channel dynamically if requested
    if len(args) > 1 and (cmd in ["setteaser", "set_teaser", "teaser"] or (cmd in ["test_announce", "test_announcement"] and (args[1].startswith("@") or args[1].startswith("-100")))):
        new_target = args[1].strip()
        db.set_setting("teaser_channel_id", new_target)
        os.environ["TEASER_CHANNEL_ID"] = new_target
        if cmd in ["setteaser", "set_teaser", "teaser"]:
            bot.reply_to(
                message,
                f"✅ <b>Teaser Channel Saved!</b>\n\n"
                f"📢 Channel: <code>{safe_html(new_target)}</code>\n\n"
                f"The channel has been permanently saved to the database. Send <code>/test_announce</code> to test the announcement."
            )
            return
            
    channel_target = db.get_setting("teaser_channel_id") or os.getenv("TEASER_CHANNEL_ID")
    if not channel_target:
        bot.reply_to(
            message,
            "ℹ️ <b>Teaser Channel Not Configured</b>\n\n"
            "To link your channel, run:\n"
            "<code>/setteaser @TheJudeLibrary</code>\n\n"
            "⚠️ <b>Note:</b> Make sure the bot is an <b>Administrator</b> in @TheJudeLibrary with permission to <b>Post Messages</b>."
        )
        return
        
    # If /teaser without arguments, show current status
    if cmd == "teaser" and len(args) == 1:
        current_num = db.get_current_customer_number()
        bot.reply_to(
            message,
            f"📢 <b>Current Teaser Channel:</b> <code>{safe_html(channel_target)}</code>\n"
            f"🔢 <b>Next Buyer Number:</b> <b>{get_ordinal(current_num)} Customer</b>\n\n"
            f"• To test: <code>/test_announce</code>\n"
            f"• To change channel: <code>/setteaser @TheJudeLibrary</code>\n"
            f"• To change number: <code>/set_customer_number 36</code>"
        )
        return
        
    # Send test announcement
    try:
        raw_target = str(channel_target).strip()
        if (raw_target.startswith("-") and raw_target[1:].isdigit()) or raw_target.isdigit():
            target_chat_id = int(raw_target)
        else:
            target_chat_id = raw_target if raw_target.startswith("@") else f"@{raw_target}"
            
        thread_id = db.get_setting("teaser_thread_id") or os.getenv("TEASER_THREAD_ID")
        kwargs = {}
        if thread_id and str(thread_id).strip().isdigit():
            kwargs["message_thread_id"] = int(thread_id.strip())
            
        current_num = db.get_current_customer_number()
        ordinal_str = get_ordinal(current_num)
        
        test_text = (
            f"<b>🎉 New Buyer Alert! 🎉</b>\n"
            f"We’re happy to announce our {ordinal_str} Customer of the Bellingham Library."
        )
        sent = bot.send_message(target_chat_id, test_text, **kwargs)
        bot.reply_to(
            message,
            f"✅ <b>Test Announcement Sent!</b>\n\n"
            f"📢 <b>Channel:</b> <code>{safe_html(channel_target)}</code>\n"
            f"🆔 <b>Message ID:</b> <code>{sent.message_id}</code>\n\n"
            f"<b>Message Preview:</b>\n<i>{test_text}</i>\n\n"
            f"The bot is successfully connected to your channel and will announce new buyers automatically whenever you authorize them."
        )
    except Exception as e:
        bot.reply_to(
            message,
            f"❌ <b>Failed to Post in Teaser Channel</b>\n\n"
            f"📢 <b>Channel:</b> <code>{safe_html(channel_target)}</code>\n"
            f"⚠️ <b>Error:</b> <code>{safe_html(str(e))}</code>\n\n"
            f"<b>Troubleshooting:</b>\n"
            f"1. Make sure the bot is added as an <b>Admin</b> in the channel.\n"
            f"2. Ensure the bot has <b>'Post Messages'</b> permission enabled."
        )

@bot.message_handler(commands=["auth", "authorize"])
def handle_auth(message):
    if not is_admin(message):
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        username_hint = f" (@{target_username})" if target_username else ""
        bot.reply_to(
            message,
            f"❌ Could not resolve user{username_hint} in cache.\n"
            "Please authorize by replying to their message in the group, or by using their Telegram User ID."
        )
        return
        
    if target_id in OWNER_IDS:
        bot.reply_to(
            message,
            f"👑 <b>Wait a minute...</b>\n\n"
            f"You are targeting an Owner (<code>{target_id}</code>)!\n"
            f"Owners inherently have infinite power and access to everything. You do not need to authorize them as a buyer."
        )
        return
        
    db.authorize_user(target_id, target_username, target_fname)
    bot_username = bot.get_me().username
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Get Started 🚀", url=f"https://t.me/{bot_username}?start=1"))
    
    bot.reply_to(
        message,
        f"✅ User <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')}, ID: <code>{target_id}</code>) has been authorized.\n\n"
        f"👋 Welcome to the Library, {safe_html(target_fname)}!\n"
        f"To claim your compilations and get access, please click the button below to start our private chat.",
        reply_markup=markup
    )
    
    # Post buyer announcement in teaser/announcement channel
    announce_new_buyer(target_id, target_username, target_fname)
    
    # Notify user in private chat
    try:
        bot.send_message(
            target_id,
            "🎉 You have been authorized by the administrator! "
            "Please send /start to register your email and begin getting compilation access."
        )
    except Exception:
        # User might not have started the bot yet
        pass
@bot.message_handler(commands=["unauth", "unauthorize"])
def handle_unauth(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) >= 2 and "@" in args[1] and "." in args[1] and not args[1].startswith("@"):
        handle_unauth_email(message)
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        username_hint = f" (@{target_username})" if target_username else ""
        bot.reply_to(
            message,
            f"❌ Could not resolve user{username_hint} in cache.\n"
            "Please unauthorize by replying to their message in the group, by Telegram User ID, or use <code>/unauth_email &lt;email&gt;</code>."
        )
        return
        
    if target_id in OWNER_IDS:
        bot.reply_to(message, "👑 You cannot unauthorize an Owner.")
        return
        
    # Unauthorize in database
    db.unauthorize_user(target_id)
    
    # Revoke access to all shared Google Drive files
    history = db.get_access_history(target_id)
    revoked_count = 0
    failed_count = 0
    failed_details = []
    
    for item in history:
        try:
            gdrive.revoke_file_or_folder(item["file_id"], item["permission_id"])
            revoked_count += 1
        except Exception as e:
            failed_count += 1
            failed_details.append(f"File ID: <code>{safe_html(item['file_id'])}</code> (Error: {safe_html(str(e))})")
            
    db.clear_access_history(target_id)
    
    response = (
        f"🚫 User <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')}, ID: <code>{target_id}</code>) has been unauthorized.\n\n"
        f"🔑 <b>Google Drive Revocation Details:</b>\n"
        f"- Revoked successfully: {revoked_count} items\n"
        f"- Failed to revoke: {failed_count} items"
    )
    if failed_details:
        response += "\n\nFailed items:\n" + "\n".join(failed_details)
        
    bot.reply_to(message, response)
    
    # Notify user in private chat
    try:
        bot.send_message(
            target_id,
            "⚠️ Your buyer authorization has been revoked by the administrator, and access to all previously shared compilations has been removed."
        )
    except Exception:
        pass
@bot.message_handler(commands=["ban"])
def handle_ban(message):
    if not is_admin(message):
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        username_hint = f" (@{target_username})" if target_username else ""
        bot.reply_to(
            message,
            f"❌ Could not resolve user{username_hint} in cache.\n"
            "Please ban by replying to their message in the group, or by using their Telegram User ID."
        )
        return
        
    if target_id in OWNER_IDS:
        bot.reply_to(message, "👑 You cannot ban an Owner.")
        return
        
    # Get email if registered
    user_info = db.get_user(target_id)
    email = user_info["email"] if user_info else None
    
    # 1. Unauthorize
    db.unauthorize_user(target_id)
    
    # 2. Add to Blacklist
    db.ban_user(target_id, email)
    
    # 3. Revoke all Google Drive files
    history = db.get_access_history(target_id)
    revoked_count = 0
    failed_count = 0
    
    for item in history:
        try:
            gdrive.revoke_file_or_folder(item["file_id"], item["permission_id"])
            revoked_count += 1
        except Exception:
            failed_count += 1
            
    db.clear_access_history(target_id)
    
    # 4. Ban from the main group physically
    ban_status = "Skipped (no ADMIN_CHAT_ID)"
    if ADMIN_CHAT_ID:
        try:
            bot.ban_chat_member(ADMIN_CHAT_ID, target_id)
            ban_status = "✅ Banned from group"
        except Exception as e:
            ban_status = f"❌ Failed to ban from group ({e})"
            
    response = (
        f"☢️ <b>USER NUKED</b> ☢️\n\n"
        f"User: <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')})\n"
        f"ID: <code>{target_id}</code>\n"
        f"Email: <code>{safe_html(email or 'None')}</code>\n\n"
        f"📋 <b>Actions Taken:</b>\n"
        f"- Blacklisted in Database: ✅\n"
        f"- Google Drive Files Revoked: {revoked_count} (Failed: {failed_count})\n"
        f"- Telegram Chat Ban: {ban_status}"
    )
    
    bot.reply_to(message, response)
@bot.message_handler(commands=["grant"])
def handle_grant(message):
    if not is_admin(message):
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        bot.reply_to(
            message,
            "❌ Please target a user by replying to their message or specifying their ID/username.\n"
            "Usage: `/grant @username [quota_limit]`"
        )
        return
        
    # Parse quota limit if provided
    args = message.text.split()
    quota_limit = 3
    if len(args) > 2 and args[2].isdigit():
        quota_limit = int(args[2])
    elif len(args) > 1 and args[1].isdigit() and not message.reply_to_message:
        # If user wrote /grant 123456789 5 (not replying, ID + count)
        if len(args) > 2 and args[2].isdigit():
            quota_limit = int(args[2])
            
    msg = bot.reply_to(message, "📝 Please reply with the **reason** for granting this quota (or type 'skip' to skip):")
    bot.register_next_step_handler(msg, process_grant_reason, target_id, target_username, target_fname, quota_limit)
def process_grant_reason(message, target_id, target_username, target_fname, quota_limit):
    reason = message.text
    db.reset_quota(target_id, quota_limit)
    
    bot.reply_to(
        message,
        f"✅ Quota reset and set to <b>{quota_limit}</b> for user <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')})."
    )
    
    # Notify user
    try:
        reason_text = f"\n\nℹ️ <b>Reason:</b> {safe_html(reason)}" if reason.lower() != 'skip' else ""
        bot.send_message(
            target_id,
            f"🎁 The administrator has reset your access quota! You can now request up to <b>{quota_limit}</b> more compilations.{reason_text}"
        )
    except Exception:
        pass
@bot.message_handler(commands=["deduct", "remove"])
def handle_deduct(message):
    if not is_admin(message):
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        bot.reply_to(
            message,
            "❌ Please target a user by replying to their message or specifying their ID/username.\n"
            "Usage: `/deduct @username [amount]`"
        )
        return
        
    # Parse amount if provided
    args = message.text.split()
    amount = 1
    if len(args) > 2 and args[2].isdigit():
        amount = int(args[2])
    elif len(args) > 1 and args[1].isdigit() and not message.reply_to_message:
        # If user wrote /deduct 123456789 2 (not replying, ID + count)
        if len(args) > 2 and args[2].isdigit():
            amount = int(args[2])
            
    msg = bot.reply_to(message, "📝 Please reply with the **reason** for deducting this quota (or type 'skip' to skip):")
    bot.register_next_step_handler(msg, process_deduct_reason, target_id, target_username, target_fname, amount)
def process_deduct_reason(message, target_id, target_username, target_fname, amount):
    reason = message.text
    db.deduct_quota(target_id, amount)
    
    bot.reply_to(
        message,
        f"➖ Deducted <b>{amount}</b> compilation access(es) from user <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')})."
    )
    
    # Notify user
    try:
        reason_text = f"\n\nℹ️ <b>Reason:</b> {safe_html(reason)}" if reason.lower() != 'skip' else ""
        bot.send_message(
            target_id,
            f"📉 The administrator has manually deducted <b>{amount}</b> from your remaining compilation quota.{reason_text}"
        )
    except Exception:
        pass
        
trending_name_cache = {}

@bot.message_handler(commands=["trending", "top"])
def handle_trending(message):
    user_id = message.from_user.id
    if not db.is_user_authorized(user_id):
        bot.reply_to(message, "❌ You are not authorized to use this bot.")
        return
        
    loading_msg = bot.reply_to(message, "⏳ <i>Calculating the most requested compilations...</i>")
    
    try:
        trending = db.get_trending_comps(limit=15)
        if not trending:
            bot.edit_message_text("No compilations have been requested yet!", chat_id=message.chat.id, message_id=loading_msg.message_id)
            return
            
        text = "🔥 <b>TOP 15 MOST REQUESTED COMPILATIONS</b> 🔥\n\n"
        for i, item in enumerate(trending):
            file_id = item["file_id"]
            count = item["request_count"]
            link = item["file_url"]
            
            if file_id not in trending_name_cache:
                name = gdrive.get_file_name(file_id)
                trending_name_cache[file_id] = name
                
            name = trending_name_cache[file_id]
            
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            medal = medals[i] if i < len(medals) else "🏅"
            
            text += f"{medal} <b>{safe_html(name)}</b>\n"
            text += f"📊 <i>Requested {count} times</i>\n"
            text += f"🔗 <a href='{link}'>Request Access</a>\n\n"
            
        text += "<i>(Tap a link and send it to me to instantly get access!)</i>"
        
        bot.edit_message_text(text, chat_id=message.chat.id, message_id=loading_msg.message_id, disable_web_page_preview=True)
    except Exception as e:
        bot.edit_message_text(f"❌ Failed to load trending list: {e}", chat_id=message.chat.id, message_id=loading_msg.message_id)

@bot.message_handler(commands=["whohas_rank"])
def handle_whohas_rank(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        bot.reply_to(message, "❌ Usage: `/whohas_rank [1-15]`\nExample: `/whohas_rank 3`")
        return
        
    rank = int(args[1])
    if rank < 1 or rank > 15:
        bot.reply_to(message, "❌ Please specify a rank between 1 and 15.")
        return
        
    loading_msg = bot.reply_to(message, f"⏳ <i>Fetching users for Rank #{rank}...</i>")
    
    try:
        trending = db.get_trending_comps(limit=15)
        if not trending or rank > len(trending):
            bot.edit_message_text(f"❌ There are only {len(trending)} trending compilations right now.", chat_id=message.chat.id, message_id=loading_msg.message_id)
            return
            
        item = trending[rank - 1]
        file_id = item["file_id"]
        
        # We can safely use cache or get_file_name
        if file_id in trending_name_cache:
            file_name = trending_name_cache[file_id]
        else:
            file_name = gdrive.get_file_name(file_id)
            trending_name_cache[file_id] = file_name
            
        users = db.get_users_by_file_id(file_id)
        if not users:
            bot.edit_message_text("ℹ️ No users found for this compilation.", chat_id=message.chat.id, message_id=loading_msg.message_id)
            return
            
        response = f"🔍 <b>Users who requested Rank #{rank}:</b>\n"
        response += f"📁 <b>{safe_html(file_name)}</b>\n\n"
        
        for i, u in enumerate(users[:30]):  # Limit to 30 to avoid huge messages hitting Telegram limit
            date_str = u["granted_at"].strftime("%Y-%m-%d %H:%M") if hasattr(u["granted_at"], "strftime") else str(u["granted_at"])
            response += f"👤 <b>{safe_html(u['first_name'])}</b> (@{safe_html(u['username'] or 'None')})\n"
            response += f"🆔 <code>{u['telegram_id']}</code> | ✉️ <code>{safe_html(u['email'])}</code>\n"
            response += f"📅 <i>{date_str}</i>\n\n"
            
        if len(users) > 30:
            response += f"<i>...and {len(users) - 30} more.</i>"
            
        bot.edit_message_text(response, chat_id=message.chat.id, message_id=loading_msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Failed to load users: {e}", chat_id=message.chat.id, message_id=loading_msg.message_id)

def parse_telegram_message_link(link):
    link = link.strip().rstrip("/")
    # Private group pattern: https://t.me/c/1234567890/123 or https://t.me/c/1234567890/5/123
    m_private = re.match(r"^https?://t\.me/c/(\d+)(?:/\d+)?/(\d+)$", link)
    if m_private:
        internal_id = m_private.group(1)
        msg_id = int(m_private.group(2))
        chat_id = int(f"-100{internal_id}")
        return chat_id, msg_id
        
    # Public group pattern: https://t.me/group_name/123
    m_public = re.match(r"^https?://t\.me/([a-zA-Z0-9_]+)/(\d+)$", link)
    if m_public:
        group_username = m_public.group(1)
        msg_id = int(m_public.group(2))
        return f"@{group_username}", msg_id
        
    return None, None

@bot.message_handler(commands=["react"])
def handle_react(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    
    # Case 1: Replying directly to a message: /react <emoji>
    if message.reply_to_message:
        if len(args) < 2:
            bot.reply_to(message, "❌ Usage: Reply to a message with `/react <emoji>`\nExample: `/react 🔥`")
            return
        emoji = args[1].strip()
        target_chat_id = message.chat.id
        target_msg_id = message.reply_to_message.message_id
    # Case 2: Link provided: /react <link> <emoji> or /react <emoji> <link>
    else:
        if len(args) < 3:
            bot.reply_to(
                message,
                "❌ <b>Usage:</b>\n"
                "• <code>/react &lt;message_link&gt; &lt;emoji&gt;</code>\n"
                "• Or reply directly to any message with <code>/react &lt;emoji&gt;</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/react https://t.me/c/4265920368/5/643 🔥</code>"
            )
            return
            
        arg1, arg2 = args[1].strip(), args[2].strip()
        if "t.me" in arg1:
            link, emoji = arg1, arg2
        elif "t.me" in arg2:
            link, emoji = arg2, arg1
        else:
            bot.reply_to(message, "❌ Could not find a valid Telegram message link in your command.")
            return
            
        target_chat_id, target_msg_id = parse_telegram_message_link(link)
        if not target_chat_id or not target_msg_id:
            bot.reply_to(message, "❌ Invalid Telegram message link format.")
            return
            
    try:
        reaction = [ReactionTypeEmoji(emoji)]
        bot.set_message_reaction(chat_id=target_chat_id, message_id=target_msg_id, reaction=reaction)
        bot.reply_to(message, f"✅ Reacted with {emoji} to message <code>{target_msg_id}</code>!")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to set reaction:\n<code>{e}</code>")

@bot.message_handler(commands=["strike"])
def handle_strike(message):
    if not is_admin(message):
        return
        
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: `/strike [user_id/username] [optional_reason]`")
        return
        
    target = args[1]
    reason = args[2] if len(args) > 2 else "Suspicious Activity"
    
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        bot.reply_to(message, "❌ Could not resolve user.")
        return
        
    if target_id in OWNER_IDS:
        bot.reply_to(message, "❌ You cannot strike an Owner.")
        return
        
    strikes = db.add_strike(target_id)
    
    if strikes < 3:
        bot.reply_to(message, f"⚠️ <b>Strike Added</b>\nUser: <b>{safe_html(target_fname)}</b> (ID: <code>{target_id}</code>)\nTotal Strikes: <b>{strikes}/3</b>\nReason: {safe_html(reason)}")
        try:
            bot.send_message(
                target_id, 
                f"⚠️ <b>WARNING: You have received a strike from the Administrators.</b>\n\n"
                f"Reason: <i>{safe_html(reason)}</i>\n\n"
                f"You currently have <b>{strikes}/3</b> strikes. If you reach 3 strikes, you will be permanently banned and lose access to all compilations."
            )
        except Exception:
            pass
    else:
        # 3 Strikes - Guillotine Protocol
        bot.reply_to(message, f"🚨 <b>GUILLOTINE PROTOCOL ACTIVATED</b> 🚨\n\nUser: <b>{safe_html(target_fname)}</b> (ID: <code>{target_id}</code>) has reached 3 strikes.\n\nExecuting permanent ban and total access wipe...")
        
        # 1. Revoke access from GDrive
        user_info = db.get_user(target_id)
        email = user_info["email"]
        if email:
            history = db.get_access_history_by_email(email)
            for record in history:
                file_id = record["file_id"]
                perm_id = record["permission_id"]
                try:
                    gdrive.revoke_file_or_folder(file_id, perm_id, email=email)
                except Exception:
                    pass
            db.clear_access_history_by_email(email)
            
        # 2. Ban from bot
        db.ban_user(target_id, email, reason=f"3 Strikes: {reason}")
        
        # 3. Try to kick from group if admin chat exists
        if ADMIN_CHAT_ID:
            try:
                bot.ban_chat_member(ADMIN_CHAT_ID, target_id)
                bot.unban_chat_member(ADMIN_CHAT_ID, target_id) # Kicks them without permanently banning from the chat
            except Exception:
                pass
                
        # 4. Notify user
        try:
            bot.send_message(
                target_id,
                f"🚫 <b>You have received your 3rd strike and have been permanently banned.</b>\n\n"
                f"All your Google Drive access has been revoked and your account is locked."
            )
        except Exception:
            pass
            
        bot.send_message(message.chat.id, f"✅ <b>Guillotine Execution Complete</b>\nUser {target_id} has been completely wiped from the library.")

@bot.message_handler(commands=["revoke_email"])
def handle_revoke_email(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: `/revoke_email [email]`")
        return
        
    email = args[1].lower().strip()
    if email in OWNER_EMAILS:
        bot.reply_to(message, f"👑 <code>{safe_html(email)}</code> is an <b>Owner Email</b> and cannot be revoked.")
        return
        
    history = db.get_access_history_by_email(email)
    
    if not history:
        bot.reply_to(message, f"ℹ️ No access records found for email: <code>{safe_html(email)}</code>")
        return
        
    status_msg = bot.reply_to(message, f"⏳ <b>Revoking {len(history)} files</b> from <code>{safe_html(email)}</code>...\nThis may take a moment.")
    
    revoked_count = 0
    failed_count = 0
    
    # Store links to show the admin
    links = []
    
    for record in history:
        file_id = record["file_id"]
        perm_id = record["permission_id"]
        file_url = record.get("file_url", "Unknown Link")
        
        try:
            success = gdrive.revoke_file_or_folder(file_id, perm_id, email=email)
            if success:
                revoked_count += 1
                links.append(f"🔗 <a href='{file_url}'>Compilation Link</a>")
            else:
                failed_count += 1
                links.append(f"❌ <a href='{file_url}'>Failed to Revoke (Not found)</a>")
        except Exception as e:
            print(f"Failed to revoke {file_id} for {email}: {e}")
            failed_count += 1
            links.append(f"❌ <a href='{file_url}'>Failed to Revoke (Error)</a>")
            
    db.clear_access_history_by_email(email)
    
    # Format the list of links (limit to 30 to avoid Telegram character limits)
    links_text = "\n".join(links[:30])
    if len(links) > 30:
        links_text += f"\n...and {len(links) - 30} more."
        
    bot.edit_message_text(
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id,
        text=f"✅ <b>Revoke Email Complete</b>\n\n"
             f"Email: <code>{safe_html(email)}</code>\n"
             f"Files Revoked: <b>{revoked_count}</b>\n"
             f"Failed: <b>{failed_count}</b>\n\n"
             f"<b>Requested Compilations:</b>\n"
             f"{links_text}\n\n"
             f"All matching records have been wiped from the database.",
        disable_web_page_preview=True,
        parse_mode="HTML"
    )

@bot.message_handler(commands=["unauth_email", "unauthorize_email"])
def handle_unauth_email(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(
            message,
            "❌ <b>Usage:</b>\n"
            "• <code>/unauth_email &lt;email&gt;</code>\n"
            "Example: <code>/unauth_email buyer@gmail.com</code>"
        )
        return
        
    email = args[1].lower().strip()
    if "@" not in email or "." not in email:
        bot.reply_to(message, "❌ Invalid email format.")
        return
        
    if email in OWNER_EMAILS:
        bot.reply_to(message, f"👑 <code>{safe_html(email)}</code> is an <b>Owner Email</b> and cannot be unauthorized.")
        return
        
    status_msg = bot.reply_to(message, f"⏳ <b>Unauthorizing email:</b> <code>{safe_html(email)}</code>...\nRevoking Drive access and updating database.")
    
    # 1. Look up user account in database
    user_info = db.get_user_by_email(email)
    target_id = None
    target_uname = None
    
    if user_info:
        target_id = user_info["telegram_id"]
        target_uname = user_info.get("username")
        db.unauthorize_user(target_id)
    else:
        db.unauthorize_user_by_email(email)
        
    # 2. Revoke all Google Drive files from access history
    history = db.get_access_history_by_email(email)
    revoked_count = 0
    failed_count = 0
    
    revoked_file_ids = set()
    for item in history:
        f_id = item["file_id"]
        p_id = item.get("permission_id")
        revoked_file_ids.add(f_id)
        try:
            success = gdrive.revoke_file_or_folder(f_id, p_id, email=email)
            if success:
                revoked_count += 1
            else:
                failed_count += 1
        except Exception as e:
            print(f"Failed to revoke {f_id} for {email}: {e}")
            failed_count += 1
            
    # Also check any tracked files on Drive to ensure no lingering access
    all_tracked = db.get_all_tracked_file_ids()
    for f_id in all_tracked:
        if f_id not in revoked_file_ids:
            try:
                if gdrive.revoke_file_or_folder(f_id, None, email=email):
                    revoked_count += 1
            except Exception:
                pass
                
    db.clear_access_history_by_email(email)
    if target_id:
        db.clear_access_history(target_id)
        
    # 3. Notify user on Telegram if found
    if target_id:
        try:
            bot.send_message(
                target_id,
                "⚠️ <b>Authorization Revoked</b>\n\n"
                "Your buyer authorization and access to all Google Drive compilations have been revoked by the administrator."
            )
        except Exception:
            pass
            
    user_status_str = f"✅ User @{safe_html(target_uname or 'User')} (ID: <code>{target_id}</code>) Unauthorized" if target_id else "ℹ️ No Telegram account registered with this email"
    
    bot.edit_message_text(
        f"🚫 <b>Email Unauthorized Successfully</b>\n\n"
        f"📧 <b>Email:</b> <code>{safe_html(email)}</code>\n"
        f"👤 <b>Account Status:</b> {user_status_str}\n"
        f"📁 <b>Drive Compilations Revoked:</b> {revoked_count} items\n"
        f"⚠️ <b>Failed / Missing:</b> {failed_count} items\n\n"
        f"All database access history and authorization for this email have been wiped.",
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id
    )

@bot.message_handler(commands=["access", "grant_access", "giveaccess"])
def handle_access(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    email = None
    link = None
    
    # 1. Check if replying to a message
    if message.reply_to_message and message.reply_to_message.text:
        reply_text = message.reply_to_message.text
        extracted = gdrive.extract_drive_id(reply_text)
        if extracted[0]:
            link = reply_text
        elif "@" in reply_text and "." in reply_text:
            for word in reply_text.split():
                if "@" in word and "." in word:
                    email = word.strip().lower()
                    break

    # 2. Parse arguments (order-independent)
    for arg in args[1:]:
        extracted = gdrive.extract_drive_id(arg)
        if extracted[0] and not link:
            link = arg
        elif "@" in arg and "." in arg and not arg.startswith("@") and not email:
            email = arg.strip().lower()

    if not email or not link:
        bot.reply_to(
            message,
            "❌ <b>Usage:</b>\n"
            "• <code>/access &lt;email&gt; &lt;gdrive_link&gt;</code>\n"
            "• Or reply directly to a link with <code>/access &lt;email&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/access buyer@gmail.com https://drive.google.com/drive/folders/1ABC...</code>"
        )
        return

    file_id, item_type = gdrive.extract_drive_id(link)
    if not file_id:
        bot.reply_to(message, "❌ Invalid Google Drive link.")
        return

    status_msg = bot.reply_to(
        message, 
        f"⏳ <b>Granting Google Drive Access...</b>\n"
        f"📧 Email: <code>{safe_html(email)}</code>"
    )

    try:
        permission_id = gdrive.share_file_or_folder(file_id, email, role="reader")
        file_name = gdrive.get_file_name(file_id)

        # Look up user if registered
        user_info = db.get_user_by_email(email)
        target_id = user_info["telegram_id"] if user_info else None
        target_uname = user_info.get("username") if user_info else None

        # Log into database access history
        db.log_access(target_id, email, file_id, link, permission_id)

        if user_info:
            buyer_str = f"@{safe_html(target_uname or 'User')} (ID: <code>{target_id}</code>)"
            # Notify buyer in Telegram
            try:
                bot.send_message(
                    target_id,
                    f"🎉 <b>Compilation Access Granted!</b>\n\n"
                    f"📁 <b>Item:</b> {safe_html(file_name)}\n"
                    f"🔗 <b>Link:</b> <a href='{link}'>Open in Google Drive</a>\n\n"
                    f"The administrator has directly granted your email access to this compilation."
                )
            except Exception:
                pass
        else:
            buyer_str = "<i>Direct Email (Not yet registered on Telegram)</i>"

        bot.edit_message_text(
            f"✅ <b>Access Granted Successfully!</b>\n\n"
            f"📁 <b>Item:</b> <code>{safe_html(file_name)}</code>\n"
            f"🔗 <b>File ID:</b> <code>{file_id}</code>\n"
            f"📧 <b>Email:</b> <code>{safe_html(email)}</code>\n"
            f"👤 <b>Buyer:</b> {buyer_str}\n"
            f"🔑 <b>Permission ID:</b> <code>{safe_html(permission_id)}</code>\n\n"
            f"Logged in database access history.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ <b>Failed to Grant Access</b>\n\n"
            f"📧 Email: <code>{safe_html(email)}</code>\n"
            f"📁 File ID: <code>{file_id}</code>\n"
            f"⚠️ <b>Error:</b> <code>{safe_html(str(e))}</code>",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id
        )

@bot.message_handler(commands=["whohas"])
def handle_whohas(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: `/whohas [google_drive_link]`")
        return
        
    link = args[1]
    file_id, item_type = gdrive.extract_drive_id(link)
    
    if not file_id:
        bot.reply_to(message, "❌ Invalid Google Drive URL.")
        return
        
    users = db.get_users_by_file_id(file_id)
    
    if not users:
        bot.reply_to(message, f"ℹ️ The database shows that **no one** has requested this compilation through the bot.")
        return
        
    lines = [f"🕵️‍♂️ <b>Leak Detective Report</b>", f"File ID: <code>{file_id}</code>", f"Total Granted: <b>{len(users)}</b>\n"]
    
    for u in users:
        t_id = u["telegram_id"]
        uname = u["username"]
        fname = u["first_name"]
        email = u["email"]
        date_str = u["granted_at"].strftime("%Y-%m-%d %H:%M") if u.get("granted_at") else "Unknown Date"
        
        # Clean up defaults
        if fname == "User" or fname == "Unknown":
            fname = None
            
        if fname and uname:
            display_name = f"<b>{safe_html(fname)}</b> (@{safe_html(uname)})"
        elif fname:
            display_name = f"<b>{safe_html(fname)}</b>"
        elif uname:
            display_name = f"<b>@{safe_html(uname)}</b>"
        else:
            display_name = f"<b>Unknown User</b>"
            
        lines.append(f"• {display_name}\n  └ 📧 <code>{safe_html(email)}</code>\n  └ 📅 {date_str} (ID: <code>{t_id}</code>)")
        
    # Split message if it exceeds Telegram's 4096 character limit
    full_text = "\n".join(lines)
    
    # Send in chunks if necessary
    for i in range(0, len(full_text), 4000):
        bot.reply_to(message, full_text[i:i+4000])

@bot.message_handler(commands=["public"])
def handle_public(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: `/public [google_drive_link]`")
        return
        
    link = args[1]
    file_id, item_type = gdrive.extract_drive_id(link)
    
    if not file_id:
        bot.reply_to(message, "❌ Invalid Google Drive URL.")
        return
        
    db.add_public_link(file_id, link)
    bot.reply_to(
        message,
        f"📢 Link marked as <b>Public</b> ({safe_html(item_type)}). Users requesting this link will be redirected to teasers without consuming quota."
    )

@bot.message_handler(commands=["changepublic", "makepublic"])
def handle_changepublic(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    link = None
    
    # 1. Check if replying to a message with a link
    if message.reply_to_message and message.reply_to_message.text:
        extracted = gdrive.extract_drive_id(message.reply_to_message.text)
        if extracted[0]:
            link = message.reply_to_message.text
            
    # 2. Check if link provided directly in command arguments
    if not link and len(args) >= 2:
        link = args[1]
        
    if not link:
        bot.reply_to(
            message,
            "❌ <b>Usage:</b>\n"
            "• <code>/changepublic &lt;google_drive_link&gt;</code>\n"
            "• Or reply directly to any message containing a Google Drive link with <code>/changepublic</code>"
        )
        return
        
    file_id, item_type = gdrive.extract_drive_id(link)
    if not file_id:
        bot.reply_to(message, "❌ Invalid Google Drive URL.")
        return
        
    loading_msg = bot.reply_to(message, f"⏳ <i>Changing permissions to Public on Google Drive and updating database...</i>")
    
    try:
        # 1. Set Google Drive permission so anyone with the link can view
        gdrive.make_file_public(file_id)
        
        # 2. Mark as public teaser in database
        db.add_public_link(file_id, link)
        
        file_name = gdrive.get_file_name(file_id)
        
        bot.edit_message_text(
            f"✅ <b>Successfully Changed to Public!</b>\n\n"
            f"📁 <b>Name:</b> <code>{safe_html(file_name)}</code>\n"
            f"🔗 <b>Type:</b> {safe_html(item_type.capitalize())}\n"
            f"🌐 <b>Google Drive:</b> Set to 'Anyone with the link can view'\n"
            f"📢 <b>Database:</b> Marked as Public (buyers won't be charged quota for this link)",
            chat_id=message.chat.id,
            message_id=loading_msg.message_id
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ <b>Failed to make link public:</b>\n<code>{safe_html(str(e))}</code>",
            chat_id=message.chat.id,
            message_id=loading_msg.message_id
        )
@bot.message_handler(commands=["broadcast", "brodcast"])
def handle_broadcast(message):
    if not is_admin(message):
        return
        
    # Restrict preview to private DMs only to avoid cluttering the group
    if message.chat.type != "private":
        bot.reply_to(message, "❌ Please use the `/broadcast` command in my private DMs, not in the group chat.")
        return
        
    text = message.text.replace("/broadcast", "", 1).replace("/brodcast", "", 1).strip()
    if not text:
        bot.reply_to(message, "❌ Usage: `/broadcast [your message]`")
        return
        
    if not ADMIN_CHAT_ID:
        bot.reply_to(message, "❌ ADMIN_CHAT_ID is not configured. Cannot send broadcast to announcement topic.")
        return
        
    pending_broadcasts[message.from_user.id] = text
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Announcements", callback_data=f"confirm_broadcast:ann:{message.from_user.id}"),
        InlineKeyboardButton("✅ Access Requests", callback_data=f"confirm_broadcast:acc:{message.from_user.id}")
    )
    markup.row(
        InlineKeyboardButton("✅ General Chat", callback_data=f"confirm_broadcast:gen:{message.from_user.id}"),
        InlineKeyboardButton("📨 DM All Buyers", callback_data=f"confirm_broadcast:dms:{message.from_user.id}")
    )
    markup.row(InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_broadcast:{message.from_user.id}"))
    
    bot.reply_to(
        message,
        f"📢 <b>Broadcast Preview:</b>\n\n{safe_html(text)}\n\n"
        f"<i>Where do you want to send this broadcast?</i>",
        reply_markup=markup
    )
@bot.message_handler(commands=["user", "lookup"])
def handle_user_lookup(message):
    if not is_admin(message):
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        bot.reply_to(
            message,
            "❌ Please target a user by replying to their message or specifying their ID/username.\n"
            "Usage: `/user @username`"
        )
        return
        
    user_info = db.get_user(target_id)
    if not user_info:
        bot.reply_to(message, "❌ User not found in the database.")
        return
        
    quota_used = user_info["quota_used"]
    max_quota = user_info["max_quota"]
    email = user_info["email"] or "Not registered"
    auth_status = "✅ Yes" if user_info["is_authorized"] else "🚫 No"
    
    recent_links = db.get_recent_access_links(target_id, limit=5)
    links_text = "\n".join([f"- {safe_html(link)}" for link in recent_links]) if recent_links else "None requested yet."
    
    text = (
        f"🔍 <b>User Profile Lookup</b>\n\n"
        f"👤 <b>Name:</b> {safe_html(target_fname)}\n"
        f"📛 <b>Username:</b> @{safe_html(target_username or 'None')}\n"
        f"🆔 <b>Telegram ID:</b> <code>{target_id}</code>\n"
        f"📧 <b>Email:</b> <code>{safe_html(email)}</code>\n"
        f"✅ <b>Authorized:</b> {auth_status}\n"
        f"📊 <b>Quota:</b> {max_quota - quota_used} remaining (Used {quota_used}/{max_quota})\n\n"
        f"📂 <b>Last 5 Requested Comps:</b>\n{links_text}"
    )
    
    bot.reply_to(message, text)
@bot.message_handler(commands=["stats"])
def handle_stats(message):
    if not is_admin(message):
        return
        
    # Restrict stats to private DMs only to avoid cluttering the group
    if message.chat.type != "private":
        bot.reply_to(message, "❌ Please use the `/stats` command in my private DMs, not in the group chat.")
        return
        
    stats = db.get_stats()
    
    lb_text = ""
    for i, user in enumerate(stats["leaderboard"], 1):
        username = f"@{user['username']}" if user['username'] else user['first_name']
        lb_text += f"{i}. {safe_html(username)} - {user['req_count']} comps\n"
        
    if not lb_text:
        lb_text = "No compilations requested yet."
        
    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 <b>Total Authorized Buyers:</b> {stats['total_authorized']}\n"
        f"🎁 <b>Total Comps Shared (All Time):</b> {stats['total_shared']}\n"
        f"📅 <b>Comps Shared (Last 7 Days):</b> {stats['shared_7_days']}\n\n"
        f"🏆 <b>Top Requesters Leaderboard:</b>\n{lb_text}"
    )
    
    bot.reply_to(message, text)

@bot.message_handler(commands=["nukelink", "nuke_link"])
def handle_nukelink(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    link = None
    if message.reply_to_message and message.reply_to_message.text:
        extracted = gdrive.extract_drive_id(message.reply_to_message.text)
        if extracted[0]:
            link = message.reply_to_message.text
    if not link and len(args) >= 2:
        link = args[1]
        
    if not link:
        bot.reply_to(
            message,
            "❌ <b>Usage:</b>\n"
            "• <code>/nukelink &lt;google_drive_link&gt;</code>\n"
            "• Or reply directly to a message containing a link with <code>/nukelink</code>"
        )
        return
        
    file_id, item_type = gdrive.extract_drive_id(link)
    if not file_id:
        bot.reply_to(message, "❌ Invalid Google Drive URL.")
        return
        
    file_name = gdrive.get_file_name(file_id)
    users = db.get_users_by_file_id(file_id)
    
    if not users:
        bot.reply_to(message, f"ℹ️ No buyers currently have access records for <b>{safe_html(file_name)}</b>.")
        return
        
    status_msg = bot.reply_to(
        message, 
        f"☢️ <b>Nuking Access for {len(users)} Buyers</b> on <b>{safe_html(file_name)}</b>...\n"
        f"Revoking Google Drive permissions and clearing database records."
    )
    
    revoked_count = 0
    failed_count = 0
    
    for u in users:
        perm_id = u.get("permission_id")
        email = u.get("email")
        if email and email.lower().strip() in OWNER_EMAILS:
            continue
        try:
            success = gdrive.revoke_file_or_folder(file_id, perm_id, email=email)
            if success:
                revoked_count += 1
            else:
                failed_count += 1
        except Exception as e:
            print(f"Failed to revoke {file_id} for {email}: {e}")
            failed_count += 1
            
    db.clear_access_history_by_file_id(file_id)
    
    bot.edit_message_text(
        f"☢️ <b>Link Nuke Complete!</b>\n\n"
        f"📁 <b>Item:</b> <code>{safe_html(file_name)}</code>\n"
        f"🔗 <b>File ID:</b> <code>{file_id}</code>\n"
        f"✅ <b>Successfully Revoked:</b> {revoked_count} buyers\n"
        f"⚠️ <b>Failed / Not Found:</b> {failed_count}\n\n"
        f"All access history for this compilation has been wiped from the database.",
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id
    )

@bot.message_handler(commands=["unregistered", "unregistered_emails", "unreg"])
def handle_unregistered(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    link = None
    if message.reply_to_message and message.reply_to_message.text:
        extracted = gdrive.extract_drive_id(message.reply_to_message.text)
        if extracted[0]:
            link = message.reply_to_message.text
    if not link and len(args) >= 2 and args[1].lower() != "all":
        extracted = gdrive.extract_drive_id(args[1])
        if extracted[0]:
            link = args[1]
            
    registered_emails = db.get_all_registered_emails()
    sa_email = gdrive.get_service_account_email()
    
    # CASE 1: SPECIFIC COMPILATION LINK SCAN
    if link:
        file_id, item_type = gdrive.extract_drive_id(link)
        if not file_id:
            bot.reply_to(message, "❌ Invalid Google Drive URL.")
            return
            
        status_msg = bot.reply_to(message, "⏳ <i>Auditing Google Drive permissions for this item...</i>")
        
        try:
            file_name = gdrive.get_file_name(file_id)
            permissions = gdrive.get_file_permissions(file_id)
            
            unregistered = []
            for p in permissions:
                p_type = p.get("type", "")
                p_role = p.get("role", "")
                email = p.get("emailAddress", "").strip().lower()
                
                if p_type == "anyone" or p_role in ["owner", "organizer"]:
                    continue
                if not email or email == sa_email or email in OWNER_EMAILS:
                    continue
                    
                if email not in registered_emails:
                    unregistered.append({
                        "email": email,
                        "role": p_role,
                        "perm_id": p.get("id")
                    })
                    
            if not unregistered:
                bot.edit_message_text(
                    f"✅ <b>Audit Clean!</b>\n\n"
                    f"📁 <b>Item:</b> <code>{safe_html(file_name)}</code>\n"
                    f"🔗 <b>File ID:</b> <code>{file_id}</code>\n\n"
                    f"No unregistered or unauthorized emails have access to this compilation.",
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id
                )
                return
                
            text = (
                f"🚨 <b>Unregistered Emails Detected ({len(unregistered)})</b>\n\n"
                f"📁 <b>Item:</b> <code>{safe_html(file_name)}</code>\n"
                f"🔗 <b>File ID:</b> <code>{file_id}</code>\n\n"
                f"The following emails have active Google Drive access but are <b>NOT registered buyers</b> in the bot database:\n\n"
            )
            
            for idx, u in enumerate(unregistered[:20], 1):
                text += f"{idx}. 📧 <code>{safe_html(u['email'])}</code> (Role: <i>{safe_html(u['role'])}</i>)\n"
                
            if len(unregistered) > 20:
                text += f"\n<i>...and {len(unregistered) - 20} more unregistered emails.</i>\n"
                
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(f"☢️ Revoke All {len(unregistered)} Unregistered", callback_data=f"revoke_unreg:{file_id}")
            )
            
            bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                reply_markup=markup
            )
        except Exception as e:
            bot.edit_message_text(f"❌ Error during compilation audit: {e}", chat_id=message.chat.id, message_id=status_msg.message_id)
        return

    # CASE 2: FULL LIBRARY AUDIT ACROSS ALL TRACKED COMPS
    file_ids = db.get_all_tracked_file_ids()
    if not file_ids:
        bot.reply_to(message, "ℹ️ No tracked compilations found in database to audit.")
        return
        
    status_msg = bot.reply_to(
        message, 
        f"⏳ <b>Scanning Google Drive Library...</b>\n"
        f"Auditing permissions across <b>{len(file_ids)}</b> tracked compilations in parallel..."
    )
    
    try:
        def fetch_file_info(fid):
            try:
                name, perms = gdrive.get_file_details(fid)
                return fid, name, perms, None
            except Exception as ex:
                return fid, "Unknown File", [], str(ex)

        file_info_list = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_file_info, fid) for fid in file_ids]
            for future in as_completed(futures):
                file_info_list.append(future.result())

        unregistered_map = {}
        total_files_scanned = len(file_ids)

        for f_id, f_name, permissions, err in file_info_list:
            for p in permissions:
                p_type = p.get("type", "")
                p_role = p.get("role", "")
                email = p.get("emailAddress", "").strip().lower()
                
                if p_type == "anyone" or p_role in ["owner", "organizer"]:
                    continue
                if not email or email == sa_email or email in OWNER_EMAILS:
                    continue
                    
                if email not in registered_emails:
                    if email not in unregistered_map:
                        unregistered_map[email] = []
                    unregistered_map[email].append({
                        "file_id": f_id,
                        "file_name": f_name,
                        "perm_id": p.get("id"),
                        "role": p_role
                    })
                    
        if not unregistered_map:
            bot.edit_message_text(
                f"✅ <b>Full Library Audit 100% Clean!</b>\n\n"
                f"📊 <b>Compilations Scanned:</b> {total_files_scanned}\n"
                f"👥 <b>Registered Authorized Buyers:</b> {len(registered_emails)}\n\n"
                f"No unregistered or unauthorized emails have access to any library compilations.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
            return
            
        total_unreg_emails = len(unregistered_map)
        total_access_instances = sum(len(v) for v in unregistered_map.values())
        
        text = (
            f"🚨 <b>Library Audit: Unregistered Access Detected!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 <b>Compilations Scanned:</b> {total_files_scanned}\n"
            f"⚠️ <b>Unregistered Emails Found:</b> <b>{total_unreg_emails}</b>\n"
            f"🔗 <b>Total Unauthorized Access Grants:</b> <b>{total_access_instances}</b>\n\n"
            f"<b>Unregistered Emails & Compromised Comps:</b>\n"
        )
        
        for idx, (email, comp_list) in enumerate(list(unregistered_map.items())[:10], 1):
            sample_names = ", ".join([c["file_name"] for c in comp_list[:2]])
            if len(comp_list) > 2:
                sample_names += f" (+{len(comp_list) - 2} more)"
            text += f"<b>{idx}. 📧 <code>{safe_html(email)}</code></b>\n   └ Access to <b>{len(comp_list)}</b> comp(s): <i>{safe_html(sample_names)}</i>\n\n"
            
        if total_unreg_emails > 10:
            text += f"<i>...and {total_unreg_emails - 10} more unregistered emails. Full details in attached CSV below.</i>\n"
            
        # Edit the existing message with the text summary
        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )
        
        # Generate CSV report and send as separate attachment
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Unregistered Email", "Compilation Name", "File ID", "Permission ID", "Role"])
        for email, comp_list in unregistered_map.items():
            for item in comp_list:
                writer.writerow([email, item["file_name"], item["file_id"], item["perm_id"] or "", item["role"]])
                
        csv_bytes = output.getvalue().encode("utf-8-sig")
        file_obj = io.BytesIO(csv_bytes)
        date_str = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        file_obj.name = f"unregistered_emails_{date_str}.csv"
        
        thread_kwargs = {}
        if hasattr(message, "message_thread_id") and message.message_thread_id:
            thread_kwargs["message_thread_id"] = message.message_thread_id
            
        bot.send_document(
            chat_id=message.chat.id,
            document=file_obj,
            caption=f"📊 <b>Detailed Audit Export:</b> {total_unreg_emails} unregistered email(s)",
            **thread_kwargs
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ <b>Failed to complete library audit:</b>\n<code>{safe_html(str(e))}</code>",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

@bot.message_handler(commands=["audit"])
def handle_audit(message):
    if not is_admin(message):
        return
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        bot.reply_to(
            message,
            "❌ <b>Usage:</b>\n"
            "• <code>/audit &lt;@username / user_id&gt;</code>\n"
            "• Or reply to a user's message with <code>/audit</code>"
        )
        return
        
    user_info = db.get_user(target_id)
    if not user_info:
        bot.reply_to(message, f"❌ User <code>{target_id}</code> not found in the database.")
        return
        
    history = db.get_access_history(target_id)
    strikes = user_info.get("strikes", 0)
    quota_used = user_info.get("quota_used", 0)
    max_quota = user_info.get("max_quota", 3)
    is_auth = "✅ Active" if user_info.get("is_authorized") else "🚫 Revoked / Not Authorized"
    email = user_info.get("email") or "None"
    pending_email = user_info.get("pending_email")
    banned = "🚨 YES (BLACKLISTED)" if db.is_banned(target_id, email) else "No"
    
    total_comps = len(history)
    first_claimed = "N/A"
    last_claimed = "N/A"
    if history:
        last_claimed_date = history[0]["granted_at"]
        first_claimed_date = history[-1]["granted_at"]
        last_claimed = last_claimed_date.strftime("%Y-%m-%d %H:%M") if hasattr(last_claimed_date, "strftime") else str(last_claimed_date)
        first_claimed = first_claimed_date.strftime("%Y-%m-%d %H:%M") if hasattr(first_claimed_date, "strftime") else str(first_claimed_date)

    audit_text = (
        f"🕵️‍♂️ <b>Security Dossier: {safe_html(target_fname)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Telegram ID:</b> <code>{target_id}</code>\n"
        f"📛 <b>Username:</b> @{safe_html(target_username or 'None')}\n"
        f"🛡️ <b>Auth Status:</b> {is_auth}\n"
        f"🚫 <b>Banned / Blacklisted:</b> {banned}\n"
        f"⚠️ <b>Strikes:</b> <b>{strikes}/3</b>\n\n"
        f"📧 <b>Registered Email:</b> <code>{safe_html(email)}</code>\n"
    )
    if pending_email:
        audit_text += f"⏳ <b>Pending Email Request:</b> <code>{safe_html(pending_email)}</code>\n"
        
    audit_text += (
        f"📊 <b>Current Quota:</b> {max_quota - quota_used} remaining (Used {quota_used}/{max_quota})\n"
        f"📁 <b>Total Comps Claimed:</b> <b>{total_comps}</b>\n"
        f"⏱️ <b>First Claim:</b> {first_claimed}\n"
        f"⏱️ <b>Latest Claim:</b> {last_claimed}\n\n"
        f"📂 <b>Claimed Compilations (Recent {min(15, total_comps)} of {total_comps}):</b>\n"
    )
    
    if history:
        for idx, item in enumerate(history[:15], 1):
            date_str = item["granted_at"].strftime("%m/%d %H:%M") if hasattr(item["granted_at"], "strftime") else "Unknown"
            audit_text += f"{idx}. <code>{item['file_id'][:14]}...</code> | 📅 {date_str}\n"
        if len(history) > 15:
            audit_text += f"<i>...and {len(history) - 15} more older claims.</i>\n"
    else:
        audit_text += "<i>No compilation claims recorded.</i>\n"
        
    bot.reply_to(message, audit_text)

@bot.message_handler(commands=["pending", "queue"])
def handle_pending(message):
    if not is_admin(message):
        return
        
    if not pending_edits_queue:
        bot.reply_to(message, "ℹ️ <b>Edit Queue Empty:</b> No video edit submissions are currently waiting for review.")
        return
        
    text = f"🎬 <b>Pending Edit Submissions ({len(pending_edits_queue)}):</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    
    for idx, (b_id, data) in enumerate(pending_edits_queue.items(), 1):
        elapsed_sec = int(time.time() - data.get("submitted_at", time.time()))
        elapsed_min = elapsed_sec // 60
        time_str = f"{elapsed_min}m ago" if elapsed_min > 0 else f"{elapsed_sec}s ago"
        
        fname = data.get("first_name", "User")
        uname = data.get("username")
        uname_str = f"@{uname}" if uname else "No username"
        
        text += (
            f"<b>{idx}. {safe_html(fname)}</b> ({safe_html(uname_str)})\n"
            f"   └ 🆔 <code>{b_id}</code>\n"
            f"   └ ⏳ Submitted: <b>{time_str}</b>\n\n"
        )
        
    text += "<i>Use the Approve/Reject buttons in your private chat to resolve them.</i>"
    bot.reply_to(message, text)

@bot.message_handler(commands=["export", "backup"])
def handle_export(message):
    if not is_admin(message):
        return
        
    status_msg = bot.reply_to(message, "⏳ <i>Generating CSV export of all authorized buyers...</i>")
    
    try:
        users = db.get_all_users_export()
        if not users:
            bot.edit_message_text("ℹ️ No authorized buyers found in database.", chat_id=message.chat.id, message_id=status_msg.message_id)
            return
            
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header row
        writer.writerow([
            "Telegram ID",
            "Username",
            "First Name",
            "Email",
            "Pending Email",
            "Quota Used",
            "Max Quota",
            "Strikes",
            "Total Comps Claimed"
        ])
        
        # Data rows
        for u in users:
            writer.writerow([
                u["telegram_id"],
                u["username"] or "",
                u["first_name"] or "",
                u["email"] or "",
                u["pending_email"] or "",
                u["quota_used"],
                u["max_quota"],
                u["strikes"],
                u["total_comps_claimed"]
            ])
            
        csv_bytes = output.getvalue().encode("utf-8-sig")
        file_obj = io.BytesIO(csv_bytes)
        date_str = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        file_obj.name = f"buyers_export_{date_str}.csv"
        
        bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        bot.send_document(
            chat_id=message.chat.id,
            document=file_obj,
            caption=f"📊 <b>Buyer Database Export</b>\nTotal Authorized Buyers: <b>{len(users)}</b>\nGenerated: <code>{date_str}</code>"
        )
    except Exception as e:
        bot.edit_message_text(f"❌ Failed to export data: {e}", chat_id=message.chat.id, message_id=status_msg.message_id)
# ----------------- CHAT MEMBER JOIN HANDLER -----------------
@bot.message_handler(content_types=["new_chat_members"])
def handle_new_members(message):
    # Only process if in the configured admin/exclusive group
    if ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        return
        
    for member in message.new_chat_members:
        # Cache username mapping
        db.save_username_mapping(member.username, member.id)
        
        # Enforce Blacklist immediately
        if db.is_banned(member.id):
            try:
                bot.ban_chat_member(ADMIN_CHAT_ID, member.id)
                bot.send_message(
                    ADMIN_CHAT_ID, 
                    f"☢️ Blacklisted user <code>{member.id}</code> attempted to join and was automatically banned."
                )
            except Exception:
                pass
            continue
            
        # Prepare auth card
        text = (
            f"🆕 <b>New Buyer Joined Group</b>\n"
            f"User: {safe_html(member.first_name)}\n"
            f"Username: @{safe_html(member.username or 'None')}\n"
            f"Telegram ID: <code>{member.id}</code>"
        )
        
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "Authorize Buyer", callback_data=f"auth_user:{member.id}"
            )
        )
        
        # Post in the Access Request thread
        send_to_admin_chat(text, reply_markup=markup)
# ----------------- CALLBACK BUTTON HANDLER -----------------
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data
    
    # 6. FAQ Menu Navigation (Accessible to ALL users)
    if data.startswith("faq_menu_"):
        page = data.split("faq_menu_")[1]
        
        if page == "main":
            text = (
                "❓ <b>Help & Frequently Asked Questions</b>\n\n"
                "Welcome to the FAQ menu! What do you need help with?"
            )
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("How everything works", callback_data="faq_menu_general"))
            markup.row(InlineKeyboardButton("How to claim access", callback_data="faq_menu_quota"))
            markup.row(InlineKeyboardButton("How to submit an edit", callback_data="faq_menu_edit"))
            markup.row(InlineKeyboardButton("How to change email", callback_data="faq_menu_email"))
            markup.row(InlineKeyboardButton("🔙 Close FAQ", callback_data="faq_menu_close"))
        elif page == "general":
            text = (
                "📖 <b>How Everything Works</b>\n\n"
                "Welcome to the Bellingham Library! I am your automated manager.\n\n"
                "You have purchased a specific number of 'Access Quotas'. Each quota allows you to permanently unlock one Google Drive compilation folder.\n\n"
                "When your quota hits 0, you must submit a video edit to prove you are actively using our resources. "
                "If the administrators approve your edit, your quota will be completely reset and you can request more compilations!"
            )
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Back to FAQ", callback_data="faq_menu_main"))
        elif page == "quota":
            text = (
                "📊 <b>How to claim access</b>\n\n"
                "1. Copy the Google Drive link of the compilation you want.\n"
                "2. Send the link directly to me here in our private chat.\n"
                "3. I will automatically share the folder with your registered email and deduct 1 from your quota!"
            )
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Back to FAQ", callback_data="faq_menu_main"))
        elif page == "edit":
            text = (
                "🎬 <b>How to submit an edit</b>\n\n"
                "Once your quota reaches 0, you must submit a video edit to prove you are using the compilations.\n\n"
                "Simply send the <b>video file</b> directly to me. I will forward it to the administrator for review. "
                "Once approved, your quota will be completely reset!"
            )
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Back to FAQ", callback_data="faq_menu_main"))
        elif page == "email":
            text = (
                "📧 <b>How to change your email</b>\n\n"
                "If you entered the wrong email or want to use a different Google account, "
                "just send the new email address to me right here.\n\n"
                "Your request will be sent to the admin for approval. Once approved, your new email will be registered."
            )
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Back to FAQ", callback_data="faq_menu_main"))
        elif page == "close":
            try:
                bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="FAQ Closed.")
            return
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return
    # All other buttons require admin permissions
    if not is_callback_admin(call):
        bot.answer_callback_query(call.id, "❌ You do not have administrator permissions.", show_alert=True)
        return
    
    # 1. Authorize User from join card
    if data.startswith("auth_user:"):
        user_id = int(data.split(":")[1])
        user_info = db.get_user(user_id)
        
        # Try to resolve names from callback message if not in DB yet
        first_name = "User"
        username = None
        if user_info:
            first_name = user_info["first_name"] or first_name
            username = user_info["username"] or username
            
        if not username or first_name == "User":
            try:
                target_chat = ADMIN_CHAT_ID or call.message.chat.id
                chat_member = bot.get_chat_member(target_chat, user_id).user
                if chat_member.first_name:
                    first_name = chat_member.first_name
                if chat_member.username:
                    username = chat_member.username
            except Exception:
                pass
            
        if user_id in OWNER_IDS:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"👑 <b>Hold up!</b>\n\nUser (ID: <code>{user_id}</code>) is a system Owner. They already have infinite access."
            )
            bot.answer_callback_query(call.id, "Owner detected. No authorization needed.")
            return
            
        db.authorize_user(user_id, username, first_name)
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ User (ID: <code>{user_id}</code>) has been authorized by @{safe_html(call.from_user.username)}."
        )
        bot.answer_callback_query(call.id, "User authorized successfully.")
        
        # Announce new buyer in teaser channel
        announce_new_buyer(user_id, username, first_name)
        
        try:
            bot.send_message(
                user_id,
                "🎉 You have been authorized by the administrator! "
                "Please send /start to register your email and start getting compilation access."
            )
        except Exception:
            pass
    # 2. Email Change approval/rejection
    elif data.startswith("approve_email:") or data.startswith("reject_email:"):
        action, user_id_str = data.split(":")
        user_id = int(user_id_str)
        user_info = db.get_user(user_id)
        
        if not user_info or not user_info["pending_email"]:
            bot.answer_callback_query(call.id, "No pending email change request found.", show_alert=True)
            return
            
        pending_email = user_info["pending_email"]
        old_email = user_info["email"]
        
        if action == "approve_email":
            db.approve_pending_email(user_id)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"✅ Approved email change for @{safe_html(user_info['username'] or 'User')}:\n<code>{safe_html(old_email)}</code> ➡️ <code>{safe_html(pending_email)}</code>"
            )
            bot.answer_callback_query(call.id, "Email change approved.")
            
            try:
                bot.send_message(
                    user_id,
                    f"✅ Your request to change your registered email has been approved.\n"
                    f"New registered email: <code>{safe_html(pending_email)}</code>"
                )
            except Exception:
                pass
        else:
            db.reject_pending_email(user_id)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"❌ Rejected email change for @{safe_html(user_info['username'] or 'User')}:\n<code>{safe_html(old_email)}</code> ➡️ <code>{safe_html(pending_email)}</code>"
            )
            bot.answer_callback_query(call.id, "Email change rejected.")
            
            try:
                bot.send_message(
                    user_id,
                    "❌ Your request to change your registered email was rejected by the administrator."
                )
            except Exception:
                pass
    # 3. Edit video review approval/rejection
    elif data.startswith("approve_edit:") or data.startswith("reject_edit:"):
        action, user_id_str = data.split(":")
        user_id = int(user_id_str)
        
        # When handled by one owner, delete from other owners and pop from queue
        sent_msgs = pending_edit_messages.pop(user_id, {})
        pending_edits_queue.pop(user_id, None)
        for owner_id, msg_id in sent_msgs.items():
            if owner_id != call.from_user.id:
                try:
                    bot.delete_message(owner_id, msg_id)
                except Exception:
                    pass
                    
        user_info = db.get_user(user_id)
        
        if not user_info:
            bot.answer_callback_query(call.id, "User not found.", show_alert=True)
            return
            
        if action == "approve_edit":
            db.reset_quota(user_id, user_info["max_quota"])
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=f"✅ Edit <b>Approved</b> for @{safe_html(user_info['username'] or 'User')}. Quota reset to {user_info['max_quota']}."
            )
            bot.answer_callback_query(call.id, "Edit approved. Quota reset.")
            
            try:
                bot.send_message(
                    user_id,
                    f"✅ Your edit submission has been approved! Your access quota has been reset to <b>{user_info['max_quota']}</b>."
                )
            except Exception:
                pass
        else:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=f"❌ Edit <b>Rejected</b> for @{safe_html(user_info['username'] or 'User')}."
            )
            bot.answer_callback_query(call.id, "Edit rejected.")
            
            try:
                bot.send_message(
                    user_id,
                    "❌ Your edit submission was rejected by the administrator. Please send a valid video edit file to reset your quota."
                )
            except Exception:
                pass
    # 4. Revoke unregistered emails callback
    elif data.startswith("revoke_unreg:"):
        if not (call.from_user.id in OWNER_IDS or is_admin(call.message)):
            bot.answer_callback_query(call.id, "Only administrators can perform this action.", show_alert=True)
            return
            
        file_id = data.split(":")[1]
        file_name = gdrive.get_file_name(file_id)
        permissions = gdrive.get_file_permissions(file_id)
        registered_emails = db.get_all_registered_emails()
        sa_email = gdrive.get_service_account_email()
        
        revoked_count = 0
        for p in permissions:
            p_type = p.get("type", "")
            p_role = p.get("role", "")
            email = p.get("emailAddress", "").strip().lower()
            
            if p_type == "anyone" or p_role in ["owner", "organizer"]:
                continue
            if not email or email == sa_email or email in OWNER_EMAILS:
                continue
                
            if email not in registered_emails:
                try:
                    gdrive.revoke_file_or_folder(file_id, p.get("id"), email=email)
                    revoked_count += 1
                except Exception as e:
                    print(f"Failed to revoke {email} on {file_id}: {e}")
                    
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"☢️ <b>Unregistered Access Revoked!</b>\n\n"
                 f"📁 <b>Item:</b> <code>{safe_html(file_name)}</code>\n"
                 f"✅ Successfully revoked <b>{revoked_count}</b> unregistered email(s) from Google Drive."
        )
        bot.answer_callback_query(call.id, f"Revoked {revoked_count} emails.")
    # 4. Broadcast confirmation/cancellation
    elif data.startswith("confirm_broadcast:") or data.startswith("cancel_broadcast:"):
        parts = data.split(":")
        action = parts[0]
        
        # Format is either confirm_broadcast:target:user_id or cancel_broadcast:user_id
        if action == "confirm_broadcast":
            target_topic = parts[1]
            user_id = int(parts[2])
        else:
            target_topic = None
            user_id = int(parts[1])
        
        # Only the person who initiated the broadcast can confirm/cancel it
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "You cannot confirm/cancel someone else's broadcast.", show_alert=True)
            return
            
        text = pending_broadcasts.pop(user_id, None)
        
        if not text:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="❌ This broadcast has expired or was already handled."
            )
            bot.answer_callback_query(call.id, "Expired broadcast.")
            return
            
        if action == "confirm_broadcast":
            try:
                # Determine which topic to send to
                if target_topic == "ann":
                    topic_id = int(os.getenv("ANNOUNCEMENT_THREAD_ID", 4))
                    topic_name = "Announcements"
                elif target_topic == "acc":
                    if not ACCESS_REQUEST_THREAD_ID:
                        raise ValueError("ACCESS_REQUEST_THREAD_ID is not set in your .env file.")
                    topic_id = ACCESS_REQUEST_THREAD_ID
                    topic_name = "Access Requests"
                elif target_topic == "gen":
                    # Put the text back so it doesn't expire before the next click
                    pending_broadcasts[user_id] = text
                    
                    # Instead of sending immediately, ask for reply mode
                    markup = InlineKeyboardMarkup()
                    markup.row(
                        InlineKeyboardButton("Send Default", callback_data=f"send_gen_default:{user_id}"),
                        InlineKeyboardButton("Reply to a Message", callback_data=f"send_gen_reply:{user_id}")
                    )
                    markup.row(InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_broadcast:{user_id}"))
                    
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=f"📢 <b>Broadcast Preview:</b>\n\n{safe_html(text)}\n\n"
                             f"<i>How do you want to send this to the General Chat?</i>",
                        reply_markup=markup
                    )
                    bot.answer_callback_query(call.id)
                    return
                elif target_topic == "dms":
                    authorized_users = db.get_all_authorized_users()
                    success_count = 0
                    for uid in authorized_users:
                        try:
                            bot.send_message(uid, f"{safe_html(text)}")
                            success_count += 1
                        except Exception:
                            pass
                    
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=f"✅ <b>Broadcast Sent to {success_count} Buyers via DM!</b>\n\n{safe_html(text)}"
                    )
                    bot.answer_callback_query(call.id, f"Sent to {success_count} users!")
                    return
                else:
                    topic_id = None
                    topic_name = "Main Chat"
                    
                bot.send_message(
                    ADMIN_CHAT_ID,
                    f"{safe_html(text)}",
                    message_thread_id=topic_id
                )
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"✅ <b>Broadcast Sent to {topic_name}!</b>\n\n{safe_html(text)}"
                )
                bot.answer_callback_query(call.id, "Broadcast sent!")
            except Exception as e:
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"❌ <b>Failed to send broadcast:</b>\n{e}"
                )
                bot.answer_callback_query(call.id, "Error sending broadcast.")
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"❌ <b>Broadcast Cancelled.</b>"
            )
            bot.answer_callback_query(call.id, "Cancelled.")
            
    # 5. General Chat Broadcast Options
    elif data.startswith("send_gen_default:") or data.startswith("send_gen_reply:"):
        action, user_id_str = data.split(":")
        user_id = int(user_id_str)
        
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "You cannot confirm someone else's broadcast.", show_alert=True)
            return
            
        if action == "send_gen_default":
            text = pending_broadcasts.pop(user_id, None)
            if not text:
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="❌ This broadcast has expired.")
                return
            try:
                bot.send_message(ADMIN_CHAT_ID, f"{safe_html(text)}", message_thread_id=5)
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"✅ <b>Broadcast Sent to General Chat!</b>\n\n{safe_html(text)}")
                bot.answer_callback_query(call.id)
            except Exception as e:
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"❌ <b>Failed to send broadcast:</b>\n{e}")
        
        elif action == "send_gen_reply":
            text = pending_broadcasts.get(user_id, None)
            if not text:
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="❌ This broadcast has expired.")
                return
            
            msg = bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="🔗 Please send me the **Message Link** from the General Chat that you want to reply to.\n\n"
                     "*(You can get this by right-clicking the message in your Telegram group and clicking 'Copy Message Link')*\n\n"
                     "Type /cancel to abort."
            )
            bot.register_next_step_handler(msg, process_broadcast_reply, user_id)
def process_broadcast_reply(message, user_id):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Broadcast cancelled.")
        pending_broadcasts.pop(user_id, None)
        return
        
    text = pending_broadcasts.pop(user_id, None)
    if not text:
        bot.reply_to(message, "❌ Broadcast expired.")
        return
        
    link = message.text.strip()
    # Link format: https://t.me/c/4265920368/5/643
    try:
        parts = link.rstrip("/").split("/")
        message_id = int(parts[-1])
        
        bot.send_message(
            ADMIN_CHAT_ID,
            f"{safe_html(text)}",
            message_thread_id=5,
            reply_to_message_id=message_id
        )
        bot.reply_to(message, f"✅ <b>Broadcast Sent to General Chat as a reply!</b>\n\n{safe_html(text)}")
    except Exception as e:
        bot.reply_to(message, f"❌ <b>Failed to send broadcast as reply:</b>\nCheck if the link is correct.\nError: {e}")
# ----------------- PRIVATE DM HANDLERS (BUYERS) -----------------
# Save user info cache on any message (especially in groups to map usernames)
def forward_mention_to_admin(message):
    if not OWNER_IDS:
        return
    text = message.text
    user = message.from_user.username or message.from_user.first_name
    chat_name = message.chat.title
    
    prompt = f"💬 <b>Bot Mentioned by @{user} in {safe_html(chat_name)}</b>\n\n{safe_html(text)}\n\n<i>What do you suggest for an answer to this? (Reply directly to this message to answer)</i>"
    
    # Send directly to all Owners' private DMs
    for owner_id in OWNER_IDS:
        try:
            msg = bot.send_message(owner_id, prompt)
            pending_ai_replies[msg.message_id] = {
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "thread_id": message.message_thread_id
            }
        except Exception as e:
            print(f"Failed to forward mention to owner {owner_id}: {e}")
@bot.message_handler(content_types=["new_chat_members"])
def handle_new_member(message):
    # Only act if this happens in the main group (Admin Chat / General Chat)
    if message.chat.id != ADMIN_CHAT_ID and message.chat.type not in ["group", "supergroup"]:
        return
        
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
            
        # ONLY welcome them if they are already authorized in the database
        if not db.is_user_authorized(new_member.id):
            continue
            
        # IMPORTANT: Telegram bots cannot DM users first. They MUST click a link to start the bot.
        # So we send a welcoming message in the group with a direct button to the bot's DMs!
        bot_username = bot.get_me().username
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Get Started 🚀", url=f"https://t.me/{bot_username}?start=1"))
        
        welcome_text = (
            f"👋 Welcome to the Library, {safe_html(new_member.first_name)}!\n\n"
            f"I am the automated manager. To claim your compilations and get access, please click the button below to start our private chat."
        )
        
        try:
            bot.reply_to(message, welcome_text, reply_markup=markup)
        except Exception as e:
            print(f"Failed to send welcome message: {e}")
@bot.message_handler(func=lambda message: True, content_types=["text", "photo", "video", "document"])
def handle_all_incoming(message):
    # Log chat IDs and thread IDs to help the owner configure their .env
    if message.chat.id == ADMIN_CHAT_ID or (message.chat.type in ["group", "supergroup"]):
        db.save_username_mapping(message.from_user.username, message.from_user.id)
        
    # Silently update their profile in the database if they interact with the bot
    db.update_user_profile(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
        
    # Check if this is an admin replying to an AI ghostwriter prompt
    if message.reply_to_message and message.reply_to_message.message_id in pending_ai_replies:
        if message.from_user.id in OWNER_IDS:
            target_info = pending_ai_replies.pop(message.reply_to_message.message_id)
            draft_text = message.text or message.caption or ""
            
            processing_msg = bot.reply_to(message, "⏳ <i>Polishing your response with AI...</i>")
            
            try:
                ai_response = gemini.enhance_text_to_ai_persona(draft_text)
                bot.send_message(
                    target_info["chat_id"],
                    ai_response,
                    message_thread_id=target_info.get("thread_id"),
                    reply_to_message_id=target_info["message_id"]
                )
                bot.edit_message_text(f"✅ <b>Sent AI response:</b>\n\n{safe_html(ai_response)}", chat_id=processing_msg.chat.id, message_id=processing_msg.message_id)
            except Exception as e:
                bot.edit_message_text(f"❌ <b>Failed to send AI response:</b>\n{e}", chat_id=processing_msg.chat.id, message_id=processing_msg.message_id)
            return
    # Check for bot mentions in group chats
    if message.chat.type in ["group", "supergroup"]:
        if message.text and BOT_USERNAME:
            if f"@{BOT_USERNAME.lower()}" in message.text.lower():
                forward_mention_to_admin(message)
                return
        # If user typed an unrecognized slash command in group, try to suggest correct one
        if message.text and message.text.startswith("/"):
            suggestion = detect_and_suggest_command(message.text)
            if suggestion:
                desc = COMMAND_MAP.get(suggestion, "")
                bot.reply_to(
                    message,
                    f"🤔 <b>Command not found. Did you mean:</b> <code>/{suggestion}</code>?\n"
                    f"<i>{safe_html(desc)}</i>"
                )
                return
    # Standard route processing for private DMs
    if message.chat.type == "private":
        process_private_message(message)
def process_private_message(message):
    user_id = message.from_user.id
    
    # HARD BLOCK FOR BANNED USERS
    if db.is_banned(user_id):
        # Don't even reply, just ignore them completely (or reply with a ban message)
        bot.reply_to(message, "🚫 You are permanently banned from using this bot.")
        return
    
    # 1. Start command
    if message.text and message.text.startswith("/start"):
        # Check authorization
        if not db.is_user_authorized(user_id):
            bot.reply_to(
                message,
                "❌ You are not authorized to use this bot.\n"
                "Please make sure you have joined the exclusive group and an administrator has authorized you."
            )
            return
            
        user_info = db.get_user(user_id)
        if not user_info["email"]:
            bot.reply_to(
                message,
                "Thanks for purchasing! please send your email."
            )
        else:
            quota_used, max_quota = db.get_quota(user_id)
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("❓ Help & FAQs", callback_data="faq_menu_main"))
            
            bot.reply_to(
                message,
                f"Welcome back! Send me a Google Drive compilation link to get access.\n\n"
                f"📧 Registered Email: <code>{safe_html(user_info['email'])}</code>\n"
                f"📊 Quota Used: <b>{quota_used} / {max_quota}</b>",
                reply_markup=markup
            )
        return
    # Check authorization for any other DMs
    if not db.is_user_authorized(user_id):
        bot.reply_to(message, "❌ Access Denied. You are not authorized.")
        return
    user_info = db.get_user(user_id)
    # 2. Email registration or change flow
    if message.text and re.match(EMAIL_REGEX, message.text.strip()):
        new_email = message.text.strip().lower()
        old_email = user_info["email"]
        
        # Enforce Blacklist on email
        if db.is_banned(user_id, email=new_email):
            bot.reply_to(message, "❌ This email address is blacklisted.")
            send_to_admin_chat(f"🚨 <b>Blacklist Alert</b>\nUser <code>{user_id}</code> attempted to register a blacklisted email: <code>{safe_html(new_email)}</code>")
            return
            
        if not old_email:
            # First time registering email
            db.register_email(user_id, new_email)
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("❓ Help & FAQs", callback_data="faq_menu_main"))
            
            bot.reply_to(
                message,
                f"✅ Email registered successfully as <code>{safe_html(new_email)}</code>!\n"
                "Now you can send Google Drive compilation links to get access.",
                reply_markup=markup
            )
        else:
            # Attempting to change email
            if old_email == new_email:
                bot.reply_to(message, f"ℹ️ <code>{safe_html(new_email)}</code> is already your registered email.")
                return
                
            db.set_pending_email(user_id, new_email)
            
            # Send approval request to admins
            text = (
                f"📧 <b>Email Change Request</b>\n"
                f"User: {safe_html(message.from_user.first_name)} (@{safe_html(message.from_user.username or 'None')})\n"
                f"ID: <code>{user_id}</code>\n\n"
                f"Old Email: <code>{safe_html(old_email)}</code>\n"
                f"New Email: <code>{safe_html(new_email)}</code>"
            )
            
            markup = InlineKeyboardMarkup()
            markup.row(
                InlineKeyboardButton("Approve", callback_data=f"approve_email:{user_id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject_email:{user_id}")
            )
            
            send_to_admin_chat(text, reply_markup=markup)
            
            bot.reply_to(
                message,
                f"⏳ Email change request submitted. Your email remains <code>{safe_html(old_email)}</code> until the administrator approves <code>{safe_html(new_email)}</code>."
            )
        return
    # 3. Google Drive Link submission
    if message.text and ("drive.google.com" in message.text):
        if not user_info["email"]:
            bot.reply_to(message, "⚠️ Please send your email address first before requesting comps.")
            return
            
        file_id, item_type = gdrive.extract_drive_id(message.text)
        if not file_id:
            bot.reply_to(message, "❌ That Google Drive link seems invalid. Please send a direct file or folder link.")
            return
            
        # Check if public link (teaser)
        if db.is_public_link(file_id):
            bot.reply_to(
                message,
                "ℹ️ This compilation has already been sent for free in our teasers! "
                "You don't need the bot's help this time. Enjoy! (Your quota was not charged.)"
            )
            return
            
        # Check if already requested previously
        if db.has_user_requested_file(user_id, file_id):
            bot.reply_to(
                message,
                "ℹ️ You have already requested access to this compilation previously! "
                "You can still access it. (Your quota was not charged again)."
            )
            return
            
        # Anti-Scraping Speed Limit (60 seconds)
        current_time = time.time()
        last_time = last_request_time.get(user_id, 0)
        if current_time - last_time < 60:
            bot.reply_to(
                message, 
                "⏳ <b>Anti-Scrape Protection:</b> Please wait 60 seconds before requesting another compilation."
            )
            text = (
                f"🚨 <b>Suspicious Scraping Alert</b>\n"
                f"User {safe_html(user_info['first_name'])} (<code>{user_id}</code>) is requesting comps too quickly (<60s apart)."
            )
            send_to_admin_chat(text)
            return
            
        last_request_time[user_id] = current_time
            
        # Check quota
        quota_used, max_quota = db.get_quota(user_id)
        if quota_used >= max_quota:
            bot.reply_to(
                message,
                f"❌ Your quota has been reached ({quota_used}/{max_quota})!\n"
                f"Please send an edit made with the previous comps in order to reset the quota. "
                f"⚠️ <b>Importantly, send it as a video file.</b>"
            )
            return
            
        # Grant Google Drive permission
        try:
            permission_id = gdrive.share_file_or_folder(file_id, user_info["email"])
            file_name = gdrive.get_file_name(file_id)
            
            # Log in database
            db.log_access(user_id, user_info["email"], file_id, message.text, permission_id)
            db.increment_quota(user_id)
            
            # Update values
            new_used, _ = db.get_quota(user_id)
            remaining_quota = max_quota - new_used
            
            success_msg = (
                f"✅ Access granted successfully!\n\n"
                f"📁 <b>Item:</b> {safe_html(file_name)}\n"
                f"📧 <b>Shared with:</b> <code>{safe_html(user_info['email'])}</code>\n"
                f"📊 <b>Remaining quota:</b> <b>{remaining_quota} / {max_quota}</b>"
            )
            
            if remaining_quota <= 0:
                success_msg += "\n\n⚠️ <b>Please send a video file of your edit now so your quota can be reset!</b>"
                
            bot.reply_to(message, success_msg)
        except Exception as e:
            bot.reply_to(
                message,
                f"❌ <b>Access Request Failed</b>\n\n"
                f"The compilation you requested isn't featured in our Drive. It is either that, or the compilation doesn't belong to our library at all.\n\n"
                f"Please ensure you are copying the link directly from the provided library list."
            )
        return
    # 4. Video upload for quota reset
    if message.content_type == "video":
        quota_used, max_quota = db.get_quota(user_id)
        if quota_used < max_quota:
            bot.reply_to(
                message,
                f"ℹ️ You do not need to send an edit right now. Remaining quota: <b>{max_quota - quota_used} / {max_quota}</b>"
            )
            return
            
        # Send to admin chat
        recent_links = db.get_recent_access_links(user_id, limit=3)
        links_text = "\n".join([f"- {safe_html(link)}" for link in recent_links]) if recent_links else "None found"
        
        caption = (
            f"🎬 <b>Edit Verification Request</b>\n"
            f"User: {safe_html(message.from_user.first_name)} (@{safe_html(message.from_user.username or 'None')})\n"
            f"ID: <code>{user_id}</code>\n"
            f"Email: <code>{safe_html(user_info['email'])}</code>\n\n"
            f"<b>Recently Requested Links:</b>\n{links_text}\n\n"
            f"Review the attached video edit to reset their access quota."
        )
        
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("Approve Edit", callback_data=f"approve_edit:{user_id}"),
            InlineKeyboardButton("Reject Edit", callback_data=f"reject_edit:{user_id}")
        )
        
        forward_video_to_admin(message, caption, reply_markup=markup, buyer_id=user_id)
        
        bot.reply_to(
            message,
            "⏳ Your edit video has been submitted to the administrator for review. "
            "You will be notified as soon as it is approved or rejected!"
        )
        return
    # Smart Typo Detection & Auto-Correction
    user_input = message.text.strip() if message.text else ""
    suggestion = detect_and_suggest_command(user_input) if user_input else None
    
    if suggestion:
        desc = COMMAND_MAP.get(suggestion, "")
        bot.reply_to(
            message,
            f"🤔 <b>Did you mean:</b> <code>/{suggestion}</code>?\n"
            f"<i>{safe_html(desc)}</i>\n\n"
            f"💡 Tap <code>/{suggestion}</code> to use it."
        )
        return
        
    # Clean fallback for unrecognized input
    input_preview = safe_html(user_input[:40]) if user_input else "file"
    bot.reply_to(
        message,
        f"❓ <b>Unrecognized input:</b> <code>{input_preview}</code>\n\n"
        f"💡 <b>What would you like to do?</b>\n"
        f"• Send a <b>Google Drive link</b> to claim a comp\n"
        f"• Send an <b>Email address</b> to register or update it\n"
        f"• Upload a <b>Video file</b> to reset your quota\n"
        f"• Tap <code>/start</code> or <code>/trending</code> for options"
    )
# Start bot
if __name__ == "__main__":
    from keep_alive import keep_alive
    
    # Start the background web server to keep the bot alive on free hosts
    keep_alive()
    
    db.init_db()
    print("Database initialized...")
    
    # Start Health Check Server in a background thread
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    def reminder_loop():
        while True:
            try:
                users_to_remind = db.get_users_due_for_reminder(hours=24)
                for user_id in users_to_remind:
                    try:
                        bot.send_message(
                            user_id,
                            "⏰ <b>Auto-Reminder:</b>\n\n"
                            "Hey! Just a reminder, you are out of quota. Have you finished your edit? "
                            "Submit your video here to get your quota back!"
                        )
                        db.mark_reminder_sent(user_id)
                    except Exception as e:
                        pass
            except Exception as e:
                pass
            time.sleep(3600) # Check every 1 hour
            
    threading.Thread(target=reminder_loop, daemon=True).start()
    
    print("Starting Telegram Bot polling...")
    bot.infinity_polling()