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

DUPLICATE_OVERLAP_THRESHOLD = float(os.environ.get("DUPLICATE_OVERLAP_THRESHOLD", "0.6"))
DUPLICATE_HOURS = int(os.environ.get("DUPLICATE_HOURS", "24"))

# --- Семантический дедуп (эмбеддинги саммари, A1) ---
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "512"))
# калибровка 2026-07-16 (tests/dedup_cases.md): max(между разными новостями)≈0.63,
# классы перекрываются. Строгий старт 0.80 (безопасный отрыв от 0.63, ~33% охвата),
# снижать к 0.72–0.75 (~44–56%) после подтверждения на живом потоке. Рычаг охвата — summary.
DUPLICATE_COSINE_THRESHOLD = float(os.environ.get("DUPLICATE_COSINE_THRESHOLD", "0.80"))

UNKNOWN_CONCEPT_EPS = float(os.environ.get("UNKNOWN_CONCEPT_EPS", "0.01"))

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

DEBUG_ALL_MESSAGES = os.environ.get("DEBUG_ALL_MESSAGES", "").lower() in ("1", "true", "yes")
