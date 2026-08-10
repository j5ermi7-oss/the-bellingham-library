import psycopg2
import psycopg2.extras
from psycopg2 import errors
import os
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Please set it in Render.")
def get_db_connection():
    # Neon might require sslmode=require, which is usually in the URL
    conn = psycopg2.connect(DATABASE_URL)
    return conn
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Username Cache
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS username_cache (
        username TEXT PRIMARY KEY,
        telegram_id BIGINT NOT NULL
    )
    """)
    
    # 2. Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        is_authorized INTEGER DEFAULT 0,
        email TEXT,
        pending_email TEXT,
        quota_used INTEGER DEFAULT 0,
        max_quota INTEGER DEFAULT 3,
        authorized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Safe migrations
    try: cursor.execute("ALTER TABLE users ADD COLUMN strikes INTEGER DEFAULT 0")
    except errors.DuplicateColumn: conn.rollback()
    except Exception: conn.rollback()
    else: conn.commit()
        
    try: cursor.execute("ALTER TABLE users ADD COLUMN zero_quota_at TIMESTAMP")
    except errors.DuplicateColumn: conn.rollback()
    except Exception: conn.rollback()
    else: conn.commit()
        
    try: cursor.execute("ALTER TABLE users ADD COLUMN reminder_sent INTEGER DEFAULT 0")
    except errors.DuplicateColumn: conn.rollback()
    except Exception: conn.rollback()
    else: conn.commit()
    
    # 3. Access History
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS access_history (
        id SERIAL PRIMARY KEY,
        telegram_id BIGINT,
        email TEXT,
        file_id TEXT,
        file_url TEXT,
        permission_id TEXT,
        granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
    )
    """)
    
    # 4. Public Links
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS public_links (
        file_id TEXT PRIMARY KEY,
        file_url TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 5. Blacklist
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS blacklist (
        telegram_id BIGINT,
        email TEXT,
        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 6. Bot Settings
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # 7. Scheduled Posts
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_posts (
        id SERIAL PRIMARY KEY,
        file_id TEXT,
        cover_file_id TEXT,
        teaser_caption TEXT,
        premium_caption TEXT,
        premium_thread_id INTEGER,
        scheduled_time TIMESTAMP,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    conn.commit()
    conn.close()
# Username Cache Operations
def save_username_mapping(username, telegram_id):
    if not username:
        return
    username = username.lower().replace("@", "")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO username_cache (username, telegram_id) 
        VALUES (%s, %s)
        ON CONFLICT (username) DO UPDATE SET telegram_id = EXCLUDED.telegram_id
        """,
        (username, telegram_id)
    )
    conn.commit()
    conn.close()
def get_id_from_username(username):
    if not username:
        return None
    username = username.lower().replace("@", "")
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT telegram_id FROM username_cache WHERE username = %s", (username,))
    row = cursor.fetchone()
    conn.close()
    return row["telegram_id"] if row else None
# User Operations
def authorize_user(telegram_id, username=None, first_name=None):
    if username:
        username = username.lower().replace("@", "")
        save_username_mapping(username, telegram_id)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT telegram_id FROM users WHERE telegram_id = %s",
        (telegram_id,)
    )
    user_exists = cursor.fetchone()
    
    if user_exists:
        cursor.execute(
            "UPDATE users SET is_authorized = 1, username = %s, first_name = %s WHERE telegram_id = %s",
            (username, first_name, telegram_id)
        )
    else:
        cursor.execute(
            "INSERT INTO users (telegram_id, username, first_name, is_authorized) VALUES (%s, %s, %s, 1)",
            (telegram_id, username, first_name)
        )
    conn.commit()
    conn.close()
def unauthorize_user(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET is_authorized = 0 WHERE telegram_id = %s",
        (telegram_id,)
    )
    conn.commit()
    conn.close()

def update_user_profile(telegram_id, username=None, first_name=None):
    if username:
        username = username.lower().replace("@", "")
        save_username_mapping(username, telegram_id)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Only update if the user exists in the database
    cursor.execute(
        "UPDATE users SET username = COALESCE(%s, username), first_name = COALESCE(%s, first_name) WHERE telegram_id = %s",
        (username, first_name, telegram_id)
    )
    conn.commit()
    conn.close()
def is_user_authorized(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT is_authorized FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row["is_authorized"]) if row else False
def get_user(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def register_email(telegram_id, email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET email = %s WHERE telegram_id = %s",
        (email, telegram_id)
    )
    conn.commit()
    conn.close()
def grant_quota(telegram_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE users 
        SET max_quota = max_quota + %s, zero_quota_at = NULL, reminder_sent = 0
        WHERE telegram_id = %s
        """,
        (amount, telegram_id)
    )
    conn.commit()
    conn.close()

def get_users_due_for_reminder(hours=24):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # Get users who hit zero quota at least 'hours' ago and haven't been reminded
    cursor.execute(
        """
        SELECT telegram_id FROM users 
        WHERE zero_quota_at IS NOT NULL 
        AND reminder_sent = 0 
        AND zero_quota_at <= CURRENT_TIMESTAMP - INTERVAL '%s hours'
        """,
        (hours,)
    )
    results = [row["telegram_id"] for row in cursor.fetchall()]
    conn.close()
    return results

def mark_reminder_sent(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET reminder_sent = 1 WHERE telegram_id = %s",
        (telegram_id,)
    )
    conn.commit()
    conn.close()

def set_pending_email(telegram_id, pending_email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET pending_email = %s WHERE telegram_id = %s",
        (pending_email, telegram_id)
    )
    conn.commit()
    conn.close()
def approve_pending_email(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT pending_email FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    if row and row["pending_email"]:
        cursor.execute(
            "UPDATE users SET email = %s, pending_email = NULL WHERE telegram_id = %s",
            (row["pending_email"], telegram_id)
        )
        conn.commit()
        success = True
    else:
        success = False
    conn.close()
    return success
def reject_pending_email(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET pending_email = NULL WHERE telegram_id = %s",
        (telegram_id,)
    )
    conn.commit()
    conn.close()
# Quota Operations
def get_quota(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT quota_used, max_quota FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return (row["quota_used"], row["max_quota"]) if row else (0, 3)
def get_stats():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT COUNT(*) as count FROM users WHERE is_authorized = 1")
    total_authorized = cursor.fetchone()["count"]
    
    cursor.execute("SELECT COUNT(*) as count FROM access_history")
    total_shared = cursor.fetchone()["count"]
    
    cursor.execute("SELECT COUNT(*) as count FROM access_history WHERE granted_at >= NOW() - INTERVAL '7 days'")
    shared_7_days = cursor.fetchone()["count"]
    
    cursor.execute("""
        SELECT u.username, u.first_name, COUNT(a.id) as req_count 
        FROM access_history a
        JOIN users u ON a.telegram_id = u.telegram_id
        GROUP BY u.telegram_id, u.username, u.first_name
        ORDER BY req_count DESC 
        LIMIT 10
    """)
    leaderboard = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "total_authorized": total_authorized,
        "total_shared": total_shared,
        "shared_7_days": shared_7_days,
        "leaderboard": leaderboard
    }
def increment_quota(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE users 
        SET quota_used = quota_used + 1,
            zero_quota_at = CASE WHEN quota_used + 1 >= max_quota THEN CURRENT_TIMESTAMP ELSE zero_quota_at END,
            reminder_sent = CASE WHEN quota_used + 1 >= max_quota THEN 0 ELSE reminder_sent END
        WHERE telegram_id = %s
        """,
        (telegram_id,)
    )
    conn.commit()
    conn.close()

def deduct_quota(telegram_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE users 
        SET quota_used = quota_used + %s,
            zero_quota_at = CASE WHEN quota_used + %s >= max_quota THEN CURRENT_TIMESTAMP ELSE zero_quota_at END,
            reminder_sent = CASE WHEN quota_used + %s >= max_quota THEN 0 ELSE reminder_sent END
        WHERE telegram_id = %s
        """,
        (amount, amount, amount, telegram_id)
    )
    conn.commit()
    conn.close()

def reset_quota(telegram_id, max_quota=3):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE users 
        SET quota_used = 0, max_quota = %s, zero_quota_at = NULL, reminder_sent = 0
        WHERE telegram_id = %s
        """,
        (max_quota, telegram_id)
    )
    conn.commit()
    conn.close()
    
def add_strike(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET strikes = strikes + 1 WHERE telegram_id = %s RETURNING strikes",
        (telegram_id,)
    )
    result = cursor.fetchone()
    conn.commit()
    conn.close()
    return result[0] if result else 0

def get_strikes(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT strikes FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row["strikes"] if row else 0

# Access History Operations
def get_trending_comps(limit=5):
    """
    Returns the top requested compilations.
    Returns: list of dicts with 'file_id', 'file_url', 'request_count'
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute(
        """
        SELECT file_id, file_url, COUNT(*) as request_count 
        FROM access_history 
        GROUP BY file_id, file_url 
        ORDER BY request_count DESC 
        LIMIT %s
        """,
        (limit,)
    )
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

def log_access(telegram_id, email, file_id, file_url, permission_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO access_history (telegram_id, email, file_id, file_url, permission_id) VALUES (%s, %s, %s, %s, %s)",
        (telegram_id, email, file_id, file_url, permission_id)
    )
    conn.commit()
    conn.close()
def get_access_history(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute(
        "SELECT file_id, file_url, permission_id, email, granted_at FROM access_history WHERE telegram_id = %s ORDER BY granted_at DESC",
        (telegram_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
    
def get_access_history_by_email(email):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute(
        "SELECT file_id, file_url, permission_id, telegram_id FROM access_history WHERE email = %s",
        (email,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
    
def get_users_by_file_id(file_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("""
        SELECT a.email, a.permission_id, a.file_url, a.granted_at, u.telegram_id, u.username, u.first_name
        FROM access_history a
        JOIN users u ON a.telegram_id = u.telegram_id
        WHERE a.file_id = %s
        ORDER BY a.granted_at DESC
    """, (file_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def has_user_requested_file(telegram_id, file_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM access_history WHERE telegram_id = %s AND file_id = %s",
        (telegram_id, file_id)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None
def get_all_authorized_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users WHERE is_authorized = 1")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]
def clear_access_history(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM access_history WHERE telegram_id = %s", (telegram_id,))
    conn.commit()
    conn.close()
    
def clear_access_history_by_email(email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM access_history WHERE email = %s", (email,))
    conn.commit()
    conn.close()

def clear_access_history_by_file_id(file_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM access_history WHERE file_id = %s", (file_id,))
    conn.commit()
    conn.close()

def get_all_users_export():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("""
        SELECT u.telegram_id, u.username, u.first_name, u.email, u.pending_email,
               u.quota_used, u.max_quota, u.is_authorized, u.strikes,
               COUNT(a.id) as total_comps_claimed
        FROM users u
        LEFT JOIN access_history a ON u.telegram_id = a.telegram_id
        WHERE u.is_authorized = 1
        GROUP BY u.telegram_id, u.username, u.first_name, u.email, u.pending_email,
                 u.quota_used, u.max_quota, u.is_authorized, u.strikes
        ORDER BY u.telegram_id ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
def get_recent_access_links(telegram_id, limit=3):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute(
        "SELECT file_url FROM access_history WHERE telegram_id = %s ORDER BY granted_at DESC LIMIT %s",
        (telegram_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [row["file_url"] for row in rows]
# Public Links Operations
def add_public_link(file_id, file_url):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO public_links (file_id, file_url) 
        VALUES (%s, %s)
        ON CONFLICT (file_id) DO UPDATE SET file_url = EXCLUDED.file_url
        """,
        (file_id, file_url)
    )
    conn.commit()
    conn.close()
def is_public_link(file_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT file_id FROM public_links WHERE file_id = %s", (file_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None
# Blacklist Operations
def ban_user(telegram_id, email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO blacklist (telegram_id, email) VALUES (%s, %s)",
        (telegram_id, email)
    )
    conn.commit()
    conn.close()
def is_banned(telegram_id, email=None):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    if email:
        cursor.execute("SELECT 1 FROM blacklist WHERE telegram_id = %s OR email = %s", (telegram_id, email))
    else:
        cursor.execute("SELECT 1 FROM blacklist WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

# Security Audit Operations
def get_all_registered_emails():
    """
    Returns a set of all registered lowercase emails of active authorized users.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT LOWER(TRIM(email)) FROM users WHERE email IS NOT NULL AND is_authorized = 1")
    rows = cursor.fetchall()
    conn.close()
    return set(row[0] for row in rows if row[0])

def get_all_tracked_file_ids():
    """
    Returns a list of all unique file_ids currently tracked in access_history or public_links.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT file_id FROM access_history WHERE file_id IS NOT NULL
        UNION 
        SELECT DISTINCT file_id FROM public_links WHERE file_id IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows if row[0]]

def get_user_by_email(email):
    """
    Looks up a user record by registered or pending email address (case-insensitive).
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute(
        "SELECT * FROM users WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) OR LOWER(TRIM(pending_email)) = LOWER(TRIM(%s))",
        (email, email)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def unauthorize_user_by_email(email):
    """
    Sets is_authorized = 0 for any user registered with the specified email.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET is_authorized = 0 WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) OR LOWER(TRIM(pending_email)) = LOWER(TRIM(%s))",
        (email, email)
    )
    conn.commit()
    conn.close()

# Bot Settings & Customer Count Operations
def get_setting(key, default=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = %s", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bot_settings (key, value)
        VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (key, str(value)))
    conn.commit()
    conn.close()

def get_current_customer_number():
    val = get_setting("customer_count")
    if val is not None:
        try:
            return int(val)
        except Exception:
            pass
    return int(os.getenv("CUSTOMER_COUNT_START", "36"))

def get_next_customer_number():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'customer_count'")
    row = cursor.fetchone()
    if not row:
        start_val = int(os.getenv("CUSTOMER_COUNT_START", "36"))
        cursor.execute("""
            INSERT INTO bot_settings (key, value)
            VALUES ('customer_count', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(start_val),))
        conn.commit()
        assigned_num = start_val
    else:
        try:
            current_num = int(row[0])
            assigned_num = current_num + 1
        except Exception:
            assigned_num = int(os.getenv("CUSTOMER_COUNT_START", "36"))
        cursor.execute("UPDATE bot_settings SET value = %s WHERE key = 'customer_count'", (str(assigned_num),))
        conn.commit()
    conn.close()
    return assigned_num

def set_customer_number(num):
    set_setting("customer_count", str(num))

# Scheduled Posts Operations
def add_scheduled_post(file_id, cover_file_id, teaser_caption, premium_caption, premium_thread_id, scheduled_time):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scheduled_posts (file_id, cover_file_id, teaser_caption, premium_caption, premium_thread_id, scheduled_time)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (file_id, cover_file_id, teaser_caption, premium_caption, premium_thread_id, scheduled_time))
    post_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return post_id

def update_scheduled_post_caption(post_id, caption_type, new_caption):
    conn = get_db_connection()
    cursor = conn.cursor()
    if caption_type == 'teaser':
        cursor.execute("UPDATE scheduled_posts SET teaser_caption = %s WHERE id = %s", (new_caption, post_id))
    elif caption_type == 'premium':
        cursor.execute("UPDATE scheduled_posts SET premium_caption = %s WHERE id = %s", (new_caption, post_id))
    conn.commit()
    conn.close()

def get_due_posts(current_time):
    """Returns pending posts where scheduled_time <= current_time"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("""
        SELECT * FROM scheduled_posts 
        WHERE status = 'pending' AND scheduled_time <= %s
    """, (current_time,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_scheduled_post(post_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM scheduled_posts WHERE id = %s", (post_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def mark_post_completed(post_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_posts SET status = 'completed' WHERE id = %s", (post_id,))
    conn.commit()
    conn.close()

def mark_post_failed(post_id, reason):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_posts SET status = 'failed', teaser_caption = CONCAT(teaser_caption, '\n\nError: ', %s) WHERE id = %s", (reason, post_id))
    conn.commit()
    conn.close()
