import os
from datetime import date

# ─── LLM Configuration (Gemini — Free) ───────────────────────────────────────
# Get a free API key at: https://aistudio.google.com/apikey
# Gemini 2.0 Flash is free with 60 requests/minute, 1500 requests/day
LLM_PROVIDER = "gemini"  # "gemini", "groq", "openai", "anthropic", or "none"
LLM_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
LLM_MODEL = "gemini-2.0-flash"
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 2000

# ─── Telegram Configuration ───────────────────────────────────────────────────
# 1. Create a bot at https://t.me/BotFather
# 2. Get your chat ID from @userinfobot on Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# ─── Agent Behaviour ──────────────────────────────────────────────────────────
TOP_NEWS_COUNT = 7
MIN_IMPACT_SCORE = 5
REQUEST_DELAY = 1.0

# ─── News Sources ─────────────────────────────────────────────────────────────
NEWS_SOURCES = {
    "moneycontrol": True,
    "economictimes": True,
    "business_standard": True,
    "livemint": True,
    "ndtv_profit": True,
    "financial_express": True,
    "the_hindu_businessline": True,
    "zeebiz": True,
}

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
TODAY = date.today().strftime("%B %d, %Y")

SCHEDULE_LABEL = {
    "0900": "9:00 AM (Pre-Market)",
    "1400": "2:00 PM (Mid-Session)",
    "1900": "7:00 PM (Post-Market)",
}
