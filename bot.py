import os
import re
import telebot
import threading
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import db
import gdrive
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
# Helper: Forward video message to admin topic/chat
def forward_video_to_admin(video_message, caption, reply_markup=None):
    if not ADMIN_CHAT_ID:
        try:
            # Re-send video file to first owner DM
            return bot.send_video(
                OWNER_IDS[0],
                video_message.video.file_id,
                caption=caption,
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Failed to forward video to owner DM: {e}")
            return None
            
    try:
        kwargs = {}
        if ACCESS_REQUEST_THREAD_ID:
            kwargs["message_thread_id"] = ACCESS_REQUEST_THREAD_ID
        return bot.send_video(
            ADMIN_CHAT_ID,
            video_message.video.file_id,
            caption=caption,
            reply_markup=reply_markup,
            **kwargs
        )
    except Exception as e:
        print(f"Failed to forward video to admin chat: {e}")
        return None
# Helper: Safely escape HTML characters for safe text insertion
def safe_html(text):
    if not text:
        return ""
    return html.escape(str(text))
# ----------------- ADMIN COMMAND HANDLERS -----------------
pending_broadcasts = {}
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
        
    db.authorize_user(target_id, target_username, target_fname)
    bot.reply_to(
        message,
        f"✅ User <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')}, ID: <code>{target_id}</code>) has been authorized."
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
            
    db.reset_quota(target_id, quota_limit)
    
    bot.reply_to(
        message,
        f"✅ Quota reset and set to <b>{quota_limit}</b> for user <b>{safe_html(target_fname)}</b> (@{safe_html(target_username or 'no_username')})."
    )
    
    # Notify user
    try:
        bot.send_message(
            target_id,
            f"🎁 The administrator has reset your access quota! You can now request up to <b>{quota_limit}</b> more compilations."
        )
    except Exception:
        pass
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
        InlineKeyboardButton("✅ Send to Announcements", callback_data=f"confirm_broadcast:{message.from_user.id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_broadcast:{message.from_user.id}")
    )
    
    bot.reply_to(
        message,
        f"📢 <b>Broadcast Preview:</b>\n\n{safe_html(text)}\n\n"
        f"<i>Do you want to send this to the Announcements topic?</i>",
        reply_markup=markup
    )
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
    if not is_callback_admin(call):
        bot.answer_callback_query(call.id, "❌ You do not have administrator permissions.", show_alert=True)
        return
        
    data = call.data
    
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
        action, user_id_str = data.split(":")
        user_id = int(user_id_str)
        
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
                topic_id = int(os.getenv("ANNOUNCEMENT_THREAD_ID", 4))
                bot.send_message(
                    ADMIN_CHAT_ID,
                    f"{safe_html(text)}",
                    message_thread_id=topic_id
                )
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"✅ <b>Broadcast Sent Successfully!</b>\n\n{safe_html(text)}"
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
# ----------------- PRIVATE DM HANDLERS (BUYERS) -----------------
# Save user info cache on any message (especially in groups to map usernames)
@bot.message_handler(func=lambda message: True, content_types=["text", "photo", "video", "document"])
def handle_all_incoming(message):
    # Log chat IDs and thread IDs to help the owner configure their .env
    if message.chat.id == ADMIN_CHAT_ID or (message.chat.type in ["group", "supergroup"]):
        db.save_username_mapping(message.from_user.username, message.from_user.id)
        print(f"Group/Topic Activity Log:")
        print(f"  Chat Title: {message.chat.title}")
        print(f"  Chat ID: {message.chat.id}")
        print(f"  Thread ID: {message.message_thread_id}")
        print(f"  User: {message.from_user.first_name} (@{message.from_user.username}, ID: {message.from_user.id})")
        
    # Standard route processing for private DMs
    if message.chat.type == "private":
        process_private_message(message)
def process_private_message(message):
    user_id = message.from_user.id
    
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
            bot.reply_to(
                message,
                f"Welcome back! Send me a Google Drive compilation link to get access.\n\n"
                f"📧 Registered Email: <code>{safe_html(user_info['email'])}</code>\n"
                f"📊 Quota Used: <b>{quota_used} / {max_quota}</b>"
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
        
        if not old_email:
            # First time registering email
            db.register_email(user_id, new_email)
            bot.reply_to(
                message,
                f"✅ Email registered successfully as <code>{safe_html(new_email)}</code>!\n"
                "Now you can send Google Drive compilation links to get access."
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
            bot.reply_to(
                message,
                f"✅ Access granted successfully!\n"
                f"Drive item shared with: <code>{safe_html(user_info['email'])}</code>\n"
                f"Remaining quota: <b>{max_quota - new_used} / {max_quota}</b>"
            )
        except Exception as e:
            bot.reply_to(
                message,
                f"❌ Failed to grant access to your Google Drive email.\n"
                f"Error details: <code>{safe_html(str(e))}</code>\n\n"
                f"💡 <b>Steps to fix:</b>\n"
                f"1. Make sure you share the compilation folder with the bot's Service Account email.\n"
                f"2. Check if your registered email <code>{safe_html(user_info['email'])}</code> is a valid Google account."
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
    db.init_db()
    print("Database initialized...")
    
    # Start Health Check Server in a background thread
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    print("Starting Telegram Bot polling...")
    bot.infinity_polling()
