import os
from dotenv import load_dotenv

load_dotenv()

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
NOTIFY_CHAT_ID = int(os.environ["NOTIFY_CHAT_ID"])
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SESSION_PATH = os.path.join(DATA_DIR, "session")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "20"))
DRIFT_EVERY_N = int(os.environ.get("DRIFT_EVERY_N", "20"))

DUPLICATE_JACCARD_THRESHOLD = float(os.environ.get("DUPLICATE_JACCARD_THRESHOLD", "0.5"))
DUPLICATE_HOURS = int(os.environ.get("DUPLICATE_HOURS", "24"))

UNKNOWN_CONCEPT_EPS = float(os.environ.get("UNKNOWN_CONCEPT_EPS", "0.01"))

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

DEBUG_ALL_MESSAGES = os.environ.get("DEBUG_ALL_MESSAGES", "").lower() in ("1", "true", "yes")
