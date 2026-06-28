import os
from dotenv import load_dotenv

load_dotenv()

ADMIN_ID = 5149883442

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
USDA_API_KEY = os.getenv("USDA_API_KEY", "")
