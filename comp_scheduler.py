import os
import re
import datetime
import pytz
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.background import BackgroundScheduler
import db
import gdrive
import json

# State storage for interactive flow
user_states = {}

def get_next_posting_time():
    """
    Returns the next closest posting time (2 AM, 12 PM, 8 PM EAT) from the current EAT time.
    """
    eat = pytz.timezone('Africa/Addis_Ababa')
    now = datetime.datetime.now(eat)
    
    # Define targets
    targets = [
        now.replace(hour=2, minute=0, second=0, microsecond=0),
        now.replace(hour=12, minute=0, second=0, microsecond=0),
        now.replace(hour=20, minute=0, second=0, microsecond=0)
    ]
    
    # Add next day's 2 AM as a target if it's past 8 PM
    targets.append((now + datetime.timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0))
    
    for t in targets:
        if t > now:
            return t
    return targets[-1]

def build_inline_keyboard(options, prefix, user_id):
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(opt, callback_data=f"{prefix}:{opt}:{user_id}") for opt in options]
    markup.add(*buttons)
    return markup

def clean_competition_from_title(title):
    # E.g. "Jude Bellingham (Home) vs. VfB Stuttgart - Bundesliga (20.11.2021)" -> "Jude Bellingham (Home) vs. VfB Stuttgart (20.11.2021)"
    # Replaces ' - Competition Name (' with ' ('
    return re.sub(r'\s*-\s*[^-]+(\s*\([\d.]+\))', r'\1', title)

def map_season_to_thread(parent_name):
    """Maps the GDrive folder name (e.g. 20-21) to the Premium channel's topic thread ID."""
    mapping = {
        "20-21": 2167,
        "21-22": 2130,
        "22-23": 1721,
        "23-24": 2168,
        "24-25": 2169,
        "25-26": 1742,
        "26-27": 1837
    }
    key = str(parent_name).strip().lower()
    return mapping.get(key, None)

def setup_scheduler(bot):
    # Background Scheduler
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Africa/Addis_Ababa'))
    
    def check_scheduled_posts():
        eat = pytz.timezone('Africa/Addis_Ababa')
        now = datetime.datetime.now(eat)
        due_posts = db.get_due_posts(now)
        
        for post in due_posts:
            try:
                # 1. Post to Premium Channel
                premium_chat_id = os.getenv("ADMIN_CHAT_ID")
                premium_thread_id = post["premium_thread_id"]
                if premium_chat_id:
                    bot.send_photo(
                        chat_id=premium_chat_id,
                        photo=post["cover_file_id"],
                        caption=post["premium_caption"],
                        parse_mode="HTML",
                        message_thread_id=premium_thread_id if premium_thread_id else None
                    )
                
                # 2. Post to Teaser Channel
                teaser_chat_id = db.get_setting("teaser_channel_id") or os.getenv("TEASER_CHANNEL_ID")
                teaser_thread_id = db.get_setting("teaser_thread_id") or os.getenv("TEASER_THREAD_ID")
                if teaser_chat_id:
                    t_kwargs = {}
                    if teaser_thread_id and str(teaser_thread_id).strip().isdigit():
                        t_kwargs["message_thread_id"] = int(str(teaser_thread_id).strip())
                    
                    bot.send_photo(
                        chat_id=teaser_chat_id,
                        photo=post["cover_file_id"],
                        caption=post["teaser_caption"],
                        parse_mode="HTML",
                        **t_kwargs
                    )
                
                db.mark_post_completed(post["id"])
            except Exception as e:
                db.mark_post_failed(post["id"], str(e))
                print(f"Error executing scheduled post {post['id']}: {e}")

    scheduler.add_job(check_scheduled_posts, 'interval', minutes=1)
    scheduler.start()

    # Handlers
    @bot.message_handler(commands=["postteaser"])
    def handle_postteaser(message):
        from bot import is_admin
        if not is_admin(message):
            return
        
        user_states[message.from_user.id] = {'step': 'waiting_for_media'}
        bot.reply_to(message, "✅ <b>Got it.</b>\nPlease send the cover photo with the Google Drive link in the caption.")

    @bot.message_handler(content_types=['photo'])
    def handle_photo(message):
        from bot import is_admin
        user_id = message.from_user.id
        
        if not is_admin(message) or user_id not in user_states or user_states[user_id].get('step') != 'waiting_for_media':
            return
            
        caption = message.caption or ""
        file_id, _ = gdrive.extract_drive_id(caption)
        
        if not file_id:
            bot.reply_to(message, "❌ <b>No Google Drive link found in the caption!</b>\nPlease try again.")
            return
            
        cover_file_id = message.photo[-1].file_id
        
        bot.reply_to(message, "⏳ <b>Analyzing compilation in Google Drive...</b>")
        
        metadata = gdrive.get_video_metadata(file_id)
        if not metadata:
            bot.reply_to(message, "❌ <b>Failed to fetch video metadata from Google Drive.</b> Check the link and permissions.")
            return
            
        user_states[user_id] = {
            'step': 'waiting_for_source',
            'file_id': file_id,
            'cover_file_id': cover_file_id,
            'gdrive_url': caption.strip(),
            'metadata': metadata
        }
        
        markup = build_inline_keyboard(["SATFEED", "HDTV", "4KTV"], "pt_src", user_id)
        msg = bot.send_message(message.chat.id, "📺 <b>Select the Source:</b>", reply_markup=markup, parse_mode="HTML")
        user_states[user_id]['msg_id'] = msg.message_id

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pt_'))
    def handle_postteaser_callback(call):
        from bot import is_callback_admin
        if not is_callback_admin(call):
            return
            
        parts = call.data.split(':')
        action = parts[0]
        value = parts[1]
        user_id = int(parts[2])
        
        if call.from_user.id != user_id or user_id not in user_states:
            bot.answer_callback_query(call.id, "Session expired or invalid.", show_alert=True)
            return
            
        state = user_states[user_id]
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        
        if action == "pt_src" and state['step'] == 'waiting_for_source':
            state['source'] = value
            state['step'] = 'waiting_for_interlacing'
            markup = build_inline_keyboard(["Deinterlaced", "Original"], "pt_int", user_id)
            bot.edit_message_text("📼 <b>Select Interlacing:</b>", chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
            
        elif action == "pt_int" and state['step'] == 'waiting_for_interlacing':
            state['interlacing'] = value
            state['step'] = 'waiting_for_commentary'
            markup = build_inline_keyboard(["English Commentary", "Stadium Sound", "Other"], "pt_com", user_id)
            bot.edit_message_text("🎙️ <b>Select Commentary:</b>", chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
            
        elif action == "pt_com" and state['step'] == 'waiting_for_commentary':
            if value == "Other":
                state['step'] = 'waiting_for_custom_commentary'
                bot.edit_message_text("✍️ <b>Please type the commentary language:</b>\n<i>(e.g., Spanish)</i>", chat_id, msg_id, parse_mode="HTML")
            else:
                state['commentary'] = value
                state['step'] = 'waiting_for_fps'
                markup = build_inline_keyboard(["25", "50", "Other"], "pt_fps", user_id)
                bot.edit_message_text("🎞️ <b>Select FPS:</b>", chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
                
        elif action == "pt_fps" and state['step'] == 'waiting_for_fps':
            if value == "Other":
                state['step'] = 'waiting_for_custom_fps'
                bot.edit_message_text("✍️ <b>Please type the custom FPS value:</b>\n<i>(e.g., 60)</i>", chat_id, msg_id, parse_mode="HTML")
            else:
                state['fps'] = value
                finalize_postteaser(bot, user_id, chat_id, msg_id)
                
    # Instead of a global interceptor that might conflict, we'll hook into bot.py's message handler directly,
    # or use a very high priority. telebot calls handlers in the order they are registered.
    # To intercept text, we'll rely on the existing bot.py sending this to us if the user is in state.
    # But for cleaner design without editing bot.py's main text handler deeply, we register it here.
    @bot.message_handler(func=lambda message: message.from_user.id in user_states and user_states[message.from_user.id].get('step') in ['waiting_for_custom_commentary', 'waiting_for_custom_fps'])
    def handle_custom_text(message):
        user_id = message.from_user.id
        state = user_states[user_id]
        
        if state['step'] == 'waiting_for_custom_commentary':
            text = message.text.strip().title()
            if not text.endswith("Commentary") and text != "Stadium Sound":
                text += " Commentary"
            state['commentary'] = text
            state['step'] = 'waiting_for_fps'
            
            markup = build_inline_keyboard(["25", "50", "Other"], "pt_fps", user_id)
            msg = bot.send_message(message.chat.id, "🎞️ <b>Select FPS:</b>", reply_markup=markup, parse_mode="HTML")
            state['msg_id'] = msg.message_id
            
        elif state['step'] == 'waiting_for_custom_fps':
            state['fps'] = ''.join(filter(str.isdigit, message.text)) or "50"
            finalize_postteaser(bot, user_id, message.chat.id, state.get('msg_id'))

def finalize_postteaser(bot, user_id, chat_id, msg_id):
    state = user_states.pop(user_id, None)
    if not state:
        return
        
    meta = state['metadata']
    base_name = meta['name'].replace(".mp4", "").replace(".mkv", "").replace(".ts", "")
    
    # Premium
    premium_caption = f"<b><a href=\"{state['gdrive_url']}\">{base_name}</a></b>"
    premium_thread_id = map_season_to_thread(meta['parent_name'])
    
    # Teaser
    clean_title = clean_competition_from_title(base_name)
    resolution = f"{meta['height']}p{state['fps']}"
    
    teaser_caption = (
        f"<b>{clean_title}</b> — {resolution} [{state['interlacing']}] | "
        f"{meta['duration_str']} | {meta['size_gb']}GB | {state['commentary']} | {state['source']}"
    )
    
    next_time = get_next_posting_time()
    
    db.add_scheduled_post(
        file_id=state['file_id'],
        cover_file_id=state['cover_file_id'],
        teaser_caption=teaser_caption,
        premium_caption=premium_caption,
        premium_thread_id=premium_thread_id,
        scheduled_time=next_time
    )
    
    time_str = next_time.strftime("%I:%M %p (EAT) on %b %d")
    
    success_msg = (
        f"✅ <b>Compilation Scheduled!</b>\n\n"
        f"🕒 <b>Posting Time:</b> {time_str}\n\n"
        f"📢 <b>Teaser Preview:</b>\n{teaser_caption}\n\n"
        f"💎 <b>Premium Preview:</b>\n{premium_caption}"
    )
    
    if msg_id:
        bot.edit_message_text(success_msg, chat_id, msg_id, parse_mode="HTML", disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, success_msg, parse_mode="HTML", disable_web_page_preview=True)
