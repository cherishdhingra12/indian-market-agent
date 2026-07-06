import os
from datetime import date

# ─── LLM Configuration (Groq — Free) ──────────────────────────────────────────
# Get a free API key at: https://console.groq.com/keys
# Groq offers Llama 3, Mixtral, and Gemma models on a generous free tier
LLM_PROVIDER = "groq"  # "gemini", "groq", "openai", "anthropic", or "none"
LLM_API_KEY = os.environ.get("GROQ_API_KEY", "YOUR_GROQ_API_KEY_HERE")
LLM_API_BASE = "https://api.groq.com/openai/v1/chat/completions"
LLM_MODEL = "llama-3.3-70b-versatile"
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 8192

# ─── Telegram Configuration ───────────────────────────────────────────────────
# 1. Create a bot at https://t.me/BotFather
# 2. Get your chat ID from @userinfobot on Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# ─── Agent Behaviour ──────────────────────────────────────────────────────────
TOP_NEWS_COUNT = 15
MIN_IMPACT_SCORE = 6
REQUEST_DELAY = 1.2

# ─── News Sources (General / Mainstream Media) ───────────────────────────────
NEWS_SOURCES = {
    "nse_announcements": True,
    "moneycontrol": True,
    "economictimes": True,
    "business_standard": True,
    "livemint": True,
    "ndtv_profit": True,
    "financial_express": True,
    "the_hindu_businessline": True,
    "zeebiz": True,
    "business_today": True,
    "inc42": True,
    "sebi": True,
    "investing_india": True,
}

# ─── Insider News Sources (Early-Signal / Exchange Filings) ───────────────────
# All public, legitimate sources that publish BEFORE mainstream media reports.
INSIDER_SOURCES = {
    "nse_bulk_deals": True,
    "nse_block_deals": True,
    "nse_insider_trading": True,
    "nse_sast": True,
    "nse_pledge": True,
    "bse_announcements": True,
    "bse_insider_trading": True,
    "sebi_orders": True,
    "pib_releases": True,
    "nse_credit_ratings": True,
}
INSIDER_TOP_NEWS_COUNT = 10

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
TODAY = date.today().strftime("%B %d, %Y")

SCHEDULE_LABEL = {
    "0900": "9:00 AM (Pre-Market)",
    "1400": "2:00 PM (Mid-Session)",
    "1900": "7:00 PM (Post-Market)",
}
