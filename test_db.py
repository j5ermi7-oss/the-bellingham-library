from db import (
    init_db,
    save_username_mapping,
    get_id_from_username,
    authorize_user,
    unauthorize_user,
    is_user_authorized,
    get_user,
    register_email,
    set_pending_email,
    approve_pending_email,
    reject_pending_email,
    get_quota,
    increment_quota,
    reset_quota,
    log_access,
    get_access_history,
    clear_access_history,
    add_public_link,
    is_public_link
)
import os

def run_tests():
    # Remove old DB if exists for a fresh test
    db_file = os.path.join(os.path.dirname(__file__), "bot_data.db")
    if os.path.exists(db_file):
        os.remove(db_file)
        
    print("Initializing Database...")
    init_db()
    assert os.path.exists(db_file), "Database file not created!"
    
    print("Testing Username Mapping...")
    save_username_mapping("TestUser", 12345)
    assert get_id_from_username("TestUser") == 12345, "Failed simple mapping"
    assert get_id_from_username("@testuser") == 12345, "Failed case/prefix mapping"
    
    print("Testing User Authorization...")
    assert not is_user_authorized(12345), "User should not be authorized yet"
    authorize_user(12345, "TestUser", "John")
    assert is_user_authorized(12345), "User authorization failed"
    
    user = get_user(12345)
    assert user["username"] == "testuser"
    assert user["first_name"] == "John"
    assert user["quota_used"] == 0
    assert user["max_quota"] == 3
    
    print("Testing Email Registration...")
    register_email(12345, "john@example.com")
    user = get_user(12345)
    assert user["email"] == "john@example.com"
    
    print("Testing Email Change flow...")
    set_pending_email(12345, "new_john@example.com")
    user = get_user(12345)
    assert user["email"] == "john@example.com"
    assert user["pending_email"] == "new_john@example.com"
    
    approve_pending_email(12345)
    user = get_user(12345)
    assert user["email"] == "new_john@example.com"
    assert user["pending_email"] is None
    
    set_pending_email(12345, "newer_john@example.com")
    reject_pending_email(12345)
    user = get_user(12345)
    assert user["email"] == "new_john@example.com"
    assert user["pending_email"] is None
    
    print("Testing Quota Operations...")
    used, max_q = get_quota(12345)
    assert used == 0 and max_q == 3
    increment_quota(12345)
    used, max_q = get_quota(12345)
    assert used == 1 and max_q == 3
    reset_quota(12345, 5)
    used, max_q = get_quota(12345)
    assert used == 0 and max_q == 5
    
    print("Testing Access History...")
    log_access(12345, "new_john@example.com", "file123", "http://gdrive/file123", "perm999")
    history = get_access_history(12345)
    assert len(history) == 1
    assert history[0]["file_id"] == "file123"
    assert history[0]["permission_id"] == "perm999"
    assert history[0]["email"] == "new_john@example.com"
    
    clear_access_history(12345)
    assert len(get_access_history(12345)) == 0
    
    print("Testing Public Links...")
    assert not is_public_link("pub456")
    add_public_link("pub456", "http://gdrive/pub456")
    assert is_public_link("pub456")
    
    print("Testing Unauthorization...")
    unauthorize_user(12345)
    assert not is_user_authorized(12345)
    
    print("\nAll database tests passed successfully!")

if __name__ == "__main__":
    run_tests()
