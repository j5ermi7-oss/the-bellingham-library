import sqlite3
import os
# Allow DB_PATH to be overridden via environment variables (vital for cloud persistent disks)
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "bot_data.db"))
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Username Cache
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS username_cache (
        username TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL
    )
    """)
    
    # 2. Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
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
    
    # 3. Access History
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS access_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
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
        telegram_id INTEGER,
        email TEXT,
        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        "INSERT OR REPLACE INTO username_cache (username, telegram_id) VALUES (?, ?)",
        (username, telegram_id)
    )
    conn.commit()
    conn.close()
def get_id_from_username(username):
    if not username:
        return None
    username = username.lower().replace("@", "")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM username_cache WHERE username = ?", (username,))
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
        "SELECT telegram_id FROM users WHERE telegram_id = ?",
        (telegram_id,)
    )
    user_exists = cursor.fetchone()
    
    if user_exists:
        cursor.execute(
            "UPDATE users SET is_authorized = 1, username = ?, first_name = ? WHERE telegram_id = ?",
            (username, first_name, telegram_id)
        )
    else:
        cursor.execute(
            "INSERT INTO users (telegram_id, username, first_name, is_authorized) VALUES (?, ?, ?, 1)",
            (telegram_id, username, first_name)
        )
    conn.commit()
    conn.close()
def unauthorize_user(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET is_authorized = 0 WHERE telegram_id = ?",
        (telegram_id,)
    )
    conn.commit()
    conn.close()
def is_user_authorized(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_authorized FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row["is_authorized"]) if row else False
def get_user(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None
def register_email(telegram_id, email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET email = ? WHERE telegram_id = ?",
        (email, telegram_id)
    )
    conn.commit()
    conn.close()
def set_pending_email(telegram_id, pending_email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET pending_email = ? WHERE telegram_id = ?",
        (pending_email, telegram_id)
    )
    conn.commit()
    conn.close()
def approve_pending_email(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT pending_email FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    if row and row["pending_email"]:
        cursor.execute(
            "UPDATE users SET email = ?, pending_email = NULL WHERE telegram_id = ?",
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
        "UPDATE users SET pending_email = NULL WHERE telegram_id = ?",
        (telegram_id,)
    )
    conn.commit()
    conn.close()
# Quota Operations
def get_quota(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT quota_used, max_quota FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return (row["quota_used"], row["max_quota"]) if row else (0, 3)
def get_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) as count FROM users WHERE is_authorized = 1")
    total_authorized = cursor.fetchone()["count"]
    
    cursor.execute("SELECT COUNT(*) as count FROM access_history")
    total_shared = cursor.fetchone()["count"]
    
    cursor.execute("SELECT COUNT(*) as count FROM access_history WHERE granted_at >= datetime('now', '-7 days')")
    shared_7_days = cursor.fetchone()["count"]
    
    cursor.execute("""
        SELECT u.username, u.first_name, COUNT(a.id) as req_count 
        FROM access_history a
        JOIN users u ON a.telegram_id = u.telegram_id
        GROUP BY a.telegram_id 
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
        "UPDATE users SET quota_used = quota_used + 1 WHERE telegram_id = ?",
        (telegram_id,)
    )
    conn.commit()
    conn.close()
def deduct_quota(telegram_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET quota_used = quota_used + ? WHERE telegram_id = ?",
        (amount, telegram_id)
    )
    conn.commit()
    conn.close()
def reset_quota(telegram_id, max_quota=3):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET quota_used = 0, max_quota = ? WHERE telegram_id = ?",
        (max_quota, telegram_id)
    )
    conn.commit()
    conn.close()
# Access History Operations
def log_access(telegram_id, email, file_id, file_url, permission_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO access_history (telegram_id, email, file_id, file_url, permission_id) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, email, file_id, file_url, permission_id)
    )
    conn.commit()
    conn.close()
def get_access_history(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT file_id, permission_id, email FROM access_history WHERE telegram_id = ?",
        (telegram_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
def clear_access_history(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM access_history WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()
def get_recent_access_links(telegram_id, limit=3):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT file_url FROM access_history WHERE telegram_id = ? ORDER BY granted_at DESC LIMIT ?",
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
        "INSERT OR REPLACE INTO public_links (file_id, file_url) VALUES (?, ?)",
        (file_id, file_url)
    )
    conn.commit()
    conn.close()
def is_public_link(file_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT file_id FROM public_links WHERE file_id = ?", (file_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None
# Blacklist Operations
def ban_user(telegram_id, email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO blacklist (telegram_id, email) VALUES (?, ?)",
        (telegram_id, email)
    )
    conn.commit()
    conn.close()
def is_banned(telegram_id, email=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if email:
        cursor.execute("SELECT 1 FROM blacklist WHERE telegram_id = ? OR email = ?", (telegram_id, email))
    else:
        cursor.execute("SELECT 1 FROM blacklist WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None
