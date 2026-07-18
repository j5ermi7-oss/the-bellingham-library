import re
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Path to the service account JSON key file
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")

# Define the scopes needed for managing file/folder permissions
SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive_service():
    """Initializes and returns the Google Drive API service."""
    # 1. Try loading from environment variable (ideal for Render/Railway/Heroku)
    env_creds = os.getenv("SERVICE_ACCOUNT_JSON")
    if env_creds:
        try:
            info = json.loads(env_creds)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
            return build("drive", "v3", credentials=creds)
        except Exception as e:
            print(f"Error loading credentials from SERVICE_ACCOUNT_JSON environment variable: {e}")
            
    # 2. Fallback to local service_account.json file
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            "Service account credentials not found. Please set the 'SERVICE_ACCOUNT_JSON' "
            "environment variable in your hosting platform, or place the 'service_account.json' "
            "file in the bot directory."
        )
    
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)

def extract_drive_id(url):
    """
    Parses Google Drive URLs and extracts the File or Folder ID.
    Returns: (id_string, type_string) or (None, None)
    """
    # 1. Match File URL patterns e.g. /file/d/[ID]/view
    file_match = re.search(r'/file/d/([a-zA-Z0-9_-]{25,50})', url)
    if file_match:
        return file_match.group(1), "file"
        
    # 2. Match Folder URL patterns e.g. /folders/[ID] or /folders/[ID]?usp=sharing
    folder_match = re.search(r'/folders/([a-zA-Z0-9_-]{25,50})', url)
    if folder_match:
        return folder_match.group(1), "folder"
        
    # 3. Match general id parameter e.g. ?id=[ID]
    id_match = re.search(r'[?&]id=([a-zA-Z0-9_-]{25,50})', url)
    if id_match:
        return id_match.group(1), "file"
        
    return None, None

def share_file_or_folder(file_id, email, role="reader"):
    """
    Shares a Google Drive file or folder with the specified email.
    Returns: permission_id (str)
    Raises: HttpError on API failure, Exception for other errors.
    """
    service = get_drive_service()
    
    user_permission = {
        "type": "user",
        "role": role,
        "emailAddress": email
    }
    
    try:
        # Create permissions
        permission = service.permissions().create(
            fileId=file_id,
            body=user_permission,
            fields="id",
            sendNotificationEmail=False
        ).execute()
        
        return permission.get("id")
    except HttpError as error:
        print(f"Google Drive API HttpError during share: {error}")
        raise error
    except Exception as error:
        print(f"Unexpected error during share: {error}")
        raise error

def revoke_file_or_folder(file_id, permission_id):
    """
    Revokes access to a Google Drive file or folder using the permission_id.
    Returns: True on success, False or raises on failure.
    """
    service = get_drive_service()
    
    try:
        service.permissions().delete(
            fileId=file_id,
            permissionId=permission_id
        ).execute()
        return True
    except HttpError as error:
        # If permission is already deleted/not found, treat as success
        if error.resp.status == 404:
            print(f"Permission {permission_id} not found on file {file_id}. Already revoked?")
            return True
        print(f"Google Drive API HttpError during revoke: {error}")
        raise error
    except Exception as error:
        print(f"Unexpected error during revoke: {error}")
        raise error
