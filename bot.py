import os
import re
import telebot
import threading
import html
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import db
import gdrive
import gemini
# Load configuration from .env file
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_IDS_STR = os.getenv("OWNER_IDS")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
ACCESS_REQUEST_THREAD_ID = os.getenv("ACCESS_REQUEST_THREAD_ID")
# Ensure required configurations are present
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing in the environment or .env file.")
if not OWNER_IDS_STR:
    raise ValueError("OWNER_IDS is missing in the environment or .env file.")
# Normalize IDs
OWNER_IDS = [int(x.strip()) for x in OWNER_IDS_STR.split(",") if x.strip()]
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
ACCESS_REQUEST_THREAD_ID = int(ACCESS_REQUEST_THREAD_ID) if ACCESS_REQUEST_THREAD_ID else None
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
# Helper: Forward video message to admin's private DM
def forward_video_to_admin(video_message, caption, reply_markup=None):
    success = False
    for owner_id in OWNER_IDS:
        try:
            bot.send_video(
                owner_id,
                video_message.video.file_id,
                caption=caption,
                reply_markup=reply_markup
            )
            success = True
        except Exception as e:
            print(f"Failed to forward video to owner {owner_id}: {e}")
    return success
# Helper: Safely escape HTML characters for safe text insertion
def safe_html(text):
    if not text:
        return ""
    return html.escape(str(text))
# ----------------- ADMIN COMMAND HANDLERS -----------------
pending_broadcasts = {}
pending_ai_replies = {}
last_request_time = {}
@bot.message_handler(commands=["refresh_menu"])
def handle_refresh_menu(message):
    if message.from_user.id not in OWNER_IDS:
        return
        
    try:
        admin_commands = [
            telebot.types.BotCommand("auth", "Authorize a user"),
            telebot.types.BotCommand("grant", "Grant quota to user"),
            telebot.types.BotCommand("deduct", "Deduct quota from user"),
            telebot.types.BotCommand("revoke", "Revoke user access"),
            telebot.types.BotCommand("revoke_email", "Revoke access by email"),
            telebot.types.BotCommand("public", "Mark a link as public teaser"),
            telebot.types.BotCommand("broadcast", "Send a broadcast"),
            telebot.types.BotCommand("user", "Lookup a user"),
            telebot.types.BotCommand("kick", "Kick from group & revoke"),
            telebot.types.BotCommand("ban", "Ban user permanently")
        ]
        
        bot.set_my_commands(admin_commands, scope=telebot.types.BotCommandScopeChat(message.chat.id))
        
        if ADMIN_CHAT_ID:
            bot.set_my_commands(admin_commands, scope=telebot.types.BotCommandScopeChatAdministrators(ADMIN_CHAT_ID))
            
        bot.reply_to(message, "✅ <b>Menu Forcefully Injected!</b>\n\nI just explicitly pinged the Telegram API to inject the commands directly into this chat. If it still doesn't appear, you may need to type `/` and wait a few seconds, or Telegram desktop might require a full restart.")
    except Exception as e:
        bot.reply_to(message, f"❌ <b>API Failed:</b>\n<code>{e}</code>")
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
        
    target_id, target_username, target_fname = resolve_target_user(message)
    if not target_id:
        username_hint = f" (@{target_username})" if target_username else ""
        bot.reply_to(
            message,
            f"❌ Could not resolve user{username_hint} in cache.\n"
            "Please unauthorize by replying to their message in the group, or by using their Telegram User ID."
        )
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
        
@bot.message_handler(commands=["revoke_email"])
def handle_revoke_email(message):
    if not is_admin(message):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: `/revoke_email [email]`")
        return
        
    email = args[1].lower().strip()
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
        disable_web_page_preview=True
    )

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
            
            # Log in database
            db.log_access(user_id, user_info["email"], file_id, message.text, permission_id)
            db.increment_quota(user_id)
            
            # Update values
            new_used, _ = db.get_quota(user_id)
            remaining_quota = max_quota - new_used
            
            success_msg = (
                f"✅ Access granted successfully!\n"
                f"Drive item shared with: <code>{safe_html(user_info['email'])}</code>\n"
                f"Remaining quota: <b>{remaining_quota} / {max_quota}</b>"
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
        
        forward_video_to_admin(message, caption, reply_markup=markup)
        
        bot.reply_to(
            message,
            "⏳ Your edit video has been submitted to the administrator for review. "
            "You will be notified as soon as it is approved or rejected!"
        )
        return
    # Catch-all reply for private DMs
    bot.reply_to(
        message,
        "❓ I didn't quite understand that.\n\n"
        "💡 <b>How to use me:</b>\n"
        "- Send a valid email address to register/change your email.\n"
        "- Send a Google Drive file/folder link to request access.\n"
        "- If your quota is reached, upload your video edit file to reset it."
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
    
    print("Starting Telegram Bot polling...")
    bot.infinity_polling()