from dotenv import load_dotenv
import os

load_dotenv()

# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def _parse_telegram_ids(value):
	return {
		int(item.strip())
		for item in value.split(",")
		if item.strip().isdigit()
	}


OWNER_ID = next(iter(_parse_telegram_ids(os.getenv("OWNER_ID", "6383171904"))), None)
ADMIN_IDS = _parse_telegram_ids(os.getenv("ADMIN_IDS", ""))
PREMIUM_PRICE_STARS = 299

# Database
DB_PATH = "english_bot.db"

# Bot settings
FREE_DAILY_LIMIT = 3
FREE_DAILY_NEW_WORD_LIMIT = 10
FREE_DAILY_AI_TEACHER_LIMIT = 5
MAX_TRANSCRIPT_LENGTH = 20000
CHUNK_SIZE = 5000

# CEFR Levels
CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
DEFAULT_LEVEL = "B1"

# Timeouts
OPENAI_TIMEOUT = 60
YT_DLP_TIMEOUT = 30