import os
import telebot
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
ACCESS_REQUEST_THREAD_ID = os.getenv("ACCESS_REQUEST_THREAD_ID")

if not BOT_TOKEN or not ADMIN_CHAT_ID:
    print("Error: BOT_TOKEN or ADMIN_CHAT_ID not found in environment.")
    exit(1)

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
ACCESS_REQUEST_THREAD_ID = int(ACCESS_REQUEST_THREAD_ID) if ACCESS_REQUEST_THREAD_ID else None

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

print(f"Attempting to send message to Chat ID: {ADMIN_CHAT_ID}, Thread ID: {ACCESS_REQUEST_THREAD_ID}...")

try:
    kwargs = {}
    if ACCESS_REQUEST_THREAD_ID:
        kwargs["message_thread_id"] = ACCESS_REQUEST_THREAD_ID
        
    msg = bot.send_message(
        ADMIN_CHAT_ID,
        "🤖 *Bellingham Compilations Bot Online*\n\nThe access request bot is now active and ready to process requests.",
        **kwargs
    )
    print(f"✅ Success! Message sent to group. Message ID: {msg.message_id}")
except Exception as e:
    print(f"❌ Failed to send message: {e}")
