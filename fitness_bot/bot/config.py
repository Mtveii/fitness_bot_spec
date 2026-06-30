import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
USDA_API_KEY = os.getenv("USDA_API_KEY", "")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

GROQ_MODEL = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-2.0-flash"

AI_TIMEOUT = 15
AI_RACE_TIMEOUT = 12
STREAM_TIMEOUT = 25

PENDING_TTL = 600
DIALOG_BUFFER_TTL = 3600
PROFILE_CACHE_TTL = 86400

NOTIFICATION_CHECK_INTERVAL_MINUTES = 15
PROFILE_RECALCULATE_INTERVAL_HOURS = 24
OBSERVATION_RETENTION_DAYS = 21
