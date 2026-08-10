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
    # Guarantees a space before the bracket even if the original didn't have one
    return re.sub(r'\s*-\s*[^-]+?\s*(\([\d.]+\))', r' \1', title)

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
                teaser_chat_id = db.get_setting("teaser_channel_id") or os.getenv("TEASER_CHANNEL_ID") or "@thejudelibrary"
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

    @bot.message_handler(commands=["cancelschedule", "cancelpost", "cancleschedule", "canclepost"])
    def handle_cancelschedule(message):
        from bot import is_admin
        if not is_admin(message):
            return
            
        import psycopg2.extras
        conn = db.get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT id FROM scheduled_posts WHERE status = 'pending'")
        pending = cursor.fetchall()
        
        if not pending:
            bot.reply_to(message, "ℹ️ There are no pending scheduled posts to cancel.")
            conn.close()
            return
            
        cursor.execute("DELETE FROM scheduled_posts WHERE status = 'pending'")
        conn.commit()
        conn.close()
        
        bot.reply_to(message, f"✅ <b>Successfully canceled {len(pending)} scheduled post(s)!</b>")
        
    @bot.message_handler(commands=["scheduled", "pendingposts", "queue"])
    def handle_view_scheduled(message):
        from bot import is_admin
        if not is_admin(message):
            return
            
        import psycopg2.extras
        conn = db.get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT id, cover_file_id, teaser_caption, premium_caption, scheduled_time FROM scheduled_posts WHERE status = 'pending' ORDER BY scheduled_time ASC")
        pending = cursor.fetchall()
        conn.close()
        
        if not pending:
            bot.reply_to(message, "📭 <b>Queue Empty:</b> There are no compilations scheduled right now.")
            return
            
        bot.reply_to(message, f"📅 <b>Scheduled Compilations ({len(pending)}):</b>", parse_mode="HTML")
        
        for idx, post in enumerate(pending, 1):
            time_str = post['scheduled_time'].strftime("%b %d, %I:%M %p (EAT)")
            
            bot.send_message(message.chat.id, f"━━━━━━━━━━━━━━━━━━━━\n🔥 <b>POST #{idx}</b> — Fires at <b>{time_str}</b>", parse_mode="HTML")
            
            bot.send_photo(
                chat_id=message.chat.id,
                photo=post['cover_file_id'],
                caption=f"📢 <b>Teaser Preview (@thejudelibrary):</b>\n\n{post['teaser_caption']}",
                parse_mode="HTML"
            )
            
            bot.send_photo(
                chat_id=message.chat.id,
                photo=post['cover_file_id'],
                caption=f"💎 <b>Premium Preview (Group):</b>\n\n{post['premium_caption']}",
                parse_mode="HTML"
            )
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(f"✏️ Edit Post #{post['id']}", callback_data=f"pt_edit:{post['id']}:{message.from_user.id}"))
            bot.send_message(message.chat.id, f"Options for Post #{post['id']}:", reply_markup=markup)

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
        if len(parts) >= 3:
            value = parts[1]
            user_id = int(parts[2])
        else:
            bot.answer_callback_query(call.id, "Invalid data.", show_alert=True)
            return
            
        state = user_states.get(user_id)
        
        # We don't require an active state for edit actions because they can be triggered from /scheduled later
        is_edit_action = action in ["pt_edit", "pt_edt_t", "pt_edt_p", "pt_edt_c"]
        
        if not state and not is_edit_action:
            bot.answer_callback_query(call.id, "Session expired or invalid.", show_alert=True)
            return
            
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
                check_fallback_requirements(bot, user_id, chat_id, msg_id)
                
        # --- NEW FALLBACK FLOWS ---
        elif action == "pt_res" and state['step'] == 'waiting_for_resolution':
            if value == "Other":
                state['step'] = 'waiting_for_custom_resolution'
                bot.edit_message_text("✍️ <b>Please type the custom Resolution:</b>\n<i>(e.g., 720)</i>", chat_id, msg_id, parse_mode="HTML")
            else:
                state['metadata']['height'] = value
                check_fallback_requirements(bot, user_id, chat_id, msg_id)
                
        elif action == "pt_dur" and state['step'] == 'waiting_for_duration':
            if value == "Other":
                state['step'] = 'waiting_for_custom_duration'
                bot.edit_message_text("✍️ <b>Please type the custom Duration:</b>\n<i>(e.g., 10:45)</i>", chat_id, msg_id, parse_mode="HTML")
            else:
                state['metadata']['duration_str'] = value
                check_fallback_requirements(bot, user_id, chat_id, msg_id)
                
        elif action == "pt_siz" and state['step'] == 'waiting_for_size':
            if value == "Other":
                state['step'] = 'waiting_for_custom_size'
                bot.edit_message_text("✍️ <b>Please type the custom Size (in GB):</b>\n<i>(e.g., 2.4)</i>", chat_id, msg_id, parse_mode="HTML")
            else:
                state['metadata']['size_gb'] = value
                check_fallback_requirements(bot, user_id, chat_id, msg_id)

        elif action == "pt_edit":
            post_id = value
            markup = InlineKeyboardMarkup()
            markup.row(
                InlineKeyboardButton("Edit Teaser", callback_data=f"pt_edt_t:{post_id}:{user_id}"),
                InlineKeyboardButton("Edit Premium", callback_data=f"pt_edt_p:{post_id}:{user_id}")
            )
            markup.add(InlineKeyboardButton("🔙 Cancel", callback_data=f"pt_edt_c:{post_id}:{user_id}"))
            bot.edit_message_text(f"What do you want to edit for Post #{post_id}?", chat_id, msg_id, reply_markup=markup)
            
        elif action == "pt_edt_t":
            post_id = value
            user_states[user_id] = {'step': 'waiting_for_edit_teaser', 'edit_post_id': post_id, 'msg_id': msg_id}
            bot.edit_message_text(f"✍️ <b>Please send the new Teaser caption for Post #{post_id}:</b>\n\n<i>(You can use HTML tags like &lt;b&gt; and &lt;a href&gt;)</i>", chat_id, msg_id, parse_mode="HTML")
            
        elif action == "pt_edt_p":
            post_id = value
            user_states[user_id] = {'step': 'waiting_for_edit_premium', 'edit_post_id': post_id, 'msg_id': msg_id}
            bot.edit_message_text(f"✍️ <b>Please send the new Premium caption for Post #{post_id}:</b>\n\n<i>(You can use HTML tags like &lt;b&gt; and &lt;a href&gt;)</i>", chat_id, msg_id, parse_mode="HTML")
            
        elif action == "pt_edt_c":
            post_id = value
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(f"✏️ Edit Post #{post_id}", callback_data=f"pt_edit:{post_id}:{user_id}"))
            bot.edit_message_text(f"Options for Post #{post_id}:", chat_id, msg_id, reply_markup=markup)

    @bot.message_handler(func=lambda message: message.from_user.id in user_states and user_states[message.from_user.id].get('step') in [
        'waiting_for_custom_commentary', 'waiting_for_custom_fps', 'waiting_for_custom_resolution', 'waiting_for_custom_duration', 'waiting_for_custom_size',
        'waiting_for_edit_teaser', 'waiting_for_edit_premium'
    ])
    def handle_custom_text(message):
        user_id = message.from_user.id
        state = user_states[user_id]
        chat_id = message.chat.id
        msg_id = state.get('msg_id')
        
        if state['step'] in ['waiting_for_edit_teaser', 'waiting_for_edit_premium']:
            post_id = state['edit_post_id']
            caption_type = 'teaser' if state['step'] == 'waiting_for_edit_teaser' else 'premium'
            new_text = message.text.strip()
            
            # Update Database
            db.update_scheduled_post_caption(post_id, caption_type, new_text)
            
            # Get updated post
            post = db.get_scheduled_post(post_id)
            
            # Clear state
            user_states.pop(user_id, None)
            
            bot.send_message(chat_id, f"✅ <b>Successfully updated the {caption_type.title()} caption for Post #{post_id}!</b>", parse_mode="HTML")
            
            # Resend previews
            bot.send_photo(
                chat_id=chat_id,
                photo=post['cover_file_id'],
                caption=f"📢 <b>Teaser Preview (@thejudelibrary):</b>\n\n{post['teaser_caption']}",
                parse_mode="HTML"
            )
            
            bot.send_photo(
                chat_id=chat_id,
                photo=post['cover_file_id'],
                caption=f"💎 <b>Premium Preview:</b>\n\n{post['premium_caption']}",
                parse_mode="HTML"
            )
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(f"✏️ Edit Post #{post_id}", callback_data=f"pt_edit:{post_id}:{user_id}"))
            bot.send_message(chat_id, f"Need to make further changes to Post #{post_id}?", reply_markup=markup)
            return

        if state['step'] == 'waiting_for_custom_commentary':
            text = message.text.strip().title()
            if not text.endswith("Commentary") and text != "Stadium Sound":
                text += " Commentary"
            state['commentary'] = text
            state['step'] = 'waiting_for_fps'
            markup = build_inline_keyboard(["25", "50", "Other"], "pt_fps", user_id)
            msg = bot.send_message(chat_id, "🎞️ <b>Select FPS:</b>", reply_markup=markup, parse_mode="HTML")
            state['msg_id'] = msg.message_id
            
        elif state['step'] == 'waiting_for_custom_fps':
            state['fps'] = ''.join(filter(str.isdigit, message.text)) or "50"
            check_fallback_requirements(bot, user_id, chat_id, msg_id)
            
        elif state['step'] == 'waiting_for_custom_resolution':
            state['metadata']['height'] = ''.join(filter(str.isdigit, message.text)) or "1080"
            check_fallback_requirements(bot, user_id, chat_id, msg_id)
            
        elif state['step'] == 'waiting_for_custom_duration':
            state['metadata']['duration_str'] = message.text.strip()
            check_fallback_requirements(bot, user_id, chat_id, msg_id)
            
        elif state['step'] == 'waiting_for_custom_size':
            # keep numbers and dots
            state['metadata']['size_gb'] = ''.join(c for c in message.text if c.isdigit() or c == '.') or "1.0"
            check_fallback_requirements(bot, user_id, chat_id, msg_id)

def check_fallback_requirements(bot, user_id, chat_id, msg_id):
    """
    Checks if metadata is missing (because it's a folder) and prompts the user.
    If everything is present, it finalizes the post.
    """
    state = user_states.get(user_id)
    if not state: return
    
    meta = state['metadata']
    
    # 1. Check Resolution
    # We default height to '1080' in gdrive.py, but if it's a folder we should ask just to be safe. 
    # Let's see if duration is unknown, meaning it's highly likely a folder.
    if meta.get('duration_str') == 'Unknown' and meta.get('height') == '1080' and 'prompted_res' not in state:
        state['prompted_res'] = True
        state['step'] = 'waiting_for_resolution'
        markup = build_inline_keyboard(["1080", "2160", "720", "Other"], "pt_res", user_id)
        if msg_id: bot.edit_message_text("📏 <b>Select Resolution:</b>", chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
        else: state['msg_id'] = bot.send_message(chat_id, "📏 <b>Select Resolution:</b>", reply_markup=markup, parse_mode="HTML").message_id
        return
        
    # 2. Check Duration
    if meta.get('duration_str') == 'Unknown':
        state['step'] = 'waiting_for_duration'
        markup = build_inline_keyboard(["15:00", "20:00", "30:00", "45:00", "Other"], "pt_dur", user_id)
        if msg_id: bot.edit_message_text("⏱️ <b>Select Duration:</b>", chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
        else: state['msg_id'] = bot.send_message(chat_id, "⏱️ <b>Select Duration:</b>", reply_markup=markup, parse_mode="HTML").message_id
        return
        
    # 3. Check Size
    if meta.get('size_gb') == 'Unknown':
        state['step'] = 'waiting_for_size'
        markup = build_inline_keyboard(["1.5", "2.0", "3.5", "5.0", "Other"], "pt_siz", user_id)
        if msg_id: bot.edit_message_text("💾 <b>Select Size (GB):</b>", chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
        else: state['msg_id'] = bot.send_message(chat_id, "💾 <b>Select Size (GB):</b>", reply_markup=markup, parse_mode="HTML").message_id
        return
        
    finalize_postteaser(bot, user_id, chat_id, msg_id)

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
    
    if state['interlacing'].lower() == "original":
        interlacing_str = ""
    else:
        interlacing_str = f"[{state['interlacing']}] | "
        
    teaser_caption = (
        f"<b>{clean_title}</b> — {resolution}\n"
        f"{interlacing_str}{meta['duration_str']} | {meta['size_gb']}GB | {state['commentary']} | {state['source']}"
    )
    
    next_time = get_next_posting_time()
    
    post_id = db.add_scheduled_post(
        file_id=state['file_id'],
        cover_file_id=state['cover_file_id'],
        teaser_caption=teaser_caption,
        premium_caption=premium_caption,
        premium_thread_id=premium_thread_id,
        scheduled_time=next_time
    )
    
    time_str = next_time.strftime("%I:%M %p (EAT) on %b %d")
    
    if msg_id:
        bot.delete_message(chat_id, msg_id)
        
    bot.send_message(chat_id, f"✅ <b>Compilation Scheduled for {time_str}!</b>", parse_mode="HTML")
    
    bot.send_photo(
        chat_id=chat_id,
        photo=state['cover_file_id'],
        caption=f"📢 <b>Teaser Preview (@thejudelibrary):</b>\n\n{teaser_caption}",
        parse_mode="HTML"
    )
    
    bot.send_photo(
        chat_id=chat_id,
        photo=state['cover_file_id'],
        caption=f"💎 <b>Premium Preview:</b>\n\n{premium_caption}",
        parse_mode="HTML"
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✏️ Edit Captions", callback_data=f"pt_edit:{post_id}:{user_id}"))
    bot.send_message(chat_id, f"Need to make changes to Post #{post_id}?", reply_markup=markup)
