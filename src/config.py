"""Configuration module for Sofia Real Estate Agent."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Base paths
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Database
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/listings.db")

# Scraping settings
SCRAPE_DELAY_MIN = float(os.getenv("SCRAPE_DELAY_MIN", "1.0"))
SCRAPE_DELAY_MAX = float(os.getenv("SCRAPE_DELAY_MAX", "3.0"))
# JSON-API sources tolerate a faster cadence than scraped HTML pages.
SCRAPE_API_DELAY_MIN = float(os.getenv("SCRAPE_API_DELAY_MIN", "0.5"))
SCRAPE_API_DELAY_MAX = float(os.getenv("SCRAPE_API_DELAY_MAX", "1.0"))
# TIN-518: sources scrape in parallel lanes (same-host sources share a lane
# so no site ever sees more than its usual serial request rate).
SCRAPE_MAX_PARALLEL_SOURCES = int(os.getenv("SCRAPE_MAX_PARALLEL_SOURCES", "6"))
MARK_INACTIVE_MIN_RATIO = float(os.getenv("MARK_INACTIVE_MIN_RATIO", "0.5"))
SOLD_AFTER_DAYS = int(os.getenv("SOLD_AFTER_DAYS", "14"))
PING_MAX_PER_RUN = int(os.getenv("PING_MAX_PER_RUN", "400"))
PING_RECENT_DAYS = int(os.getenv("PING_RECENT_DAYS", "14"))
PING_DELAY_SECONDS = float(os.getenv("PING_DELAY_SECONDS", "2.0"))
DATA_HEALTH_DRIFT_PCT = float(os.getenv("DATA_HEALTH_DRIFT_PCT", "30"))
DATA_HEALTH_BENCHMARK_DELTA_PCT = float(os.getenv("DATA_HEALTH_BENCHMARK_DELTA_PCT", "40"))
DATA_HEALTH_IMAGE_WARN_PCT = float(os.getenv("DATA_HEALTH_IMAGE_WARN_PCT", "60"))
DATA_HEALTH_IMAGE_ERROR_PCT = float(os.getenv("DATA_HEALTH_IMAGE_ERROR_PCT", "30"))
ENRICH_MAX_PER_RUN = int(os.getenv("ENRICH_MAX_PER_RUN", "500"))
ENRICH_DELAY_SECONDS = float(os.getenv("ENRICH_DELAY_SECONDS", "2.0"))

# LLM extraction defaults off when no Anthropic key is configured. An
# explicit local provider remains available without an API key.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_llm_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
LLM_PROVIDER = _llm_provider or ("anthropic" if ANTHROPIC_API_KEY else "off")
if LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
    LLM_PROVIDER = "off"
ANTHROPIC_LLM_MODEL = os.getenv("ANTHROPIC_LLM_MODEL", "claude-haiku-4-5")
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen2.5:7b")
# Nightly reads are deals-first (see llm_extract), so a small cap still
# covers every new deal (~19/day) plus the freshest ads. $2/day was sized
# for the one-time backlog and would bill ~$60/month forever; $0.25 lands
# around $3-7/month. Raise via env for a deliberate catch-up run.
LLM_MAX_PER_RUN = int(os.getenv("LLM_MAX_PER_RUN", "60"))
LLM_DAILY_BUDGET_USD = float(os.getenv("LLM_DAILY_BUDGET_USD", "0.25"))
ANTHROPIC_INPUT_USD_PER_MTOK = float(os.getenv("ANTHROPIC_INPUT_USD_PER_MTOK", "1"))
ANTHROPIC_OUTPUT_USD_PER_MTOK = float(os.getenv("ANTHROPIC_OUTPUT_USD_PER_MTOK", "5"))

# Moonshot (Kimi) — OpenAI-compatible API. Rates below are k2.6's published
# card (platform.kimi.ai/docs/pricing/chat-k26, checked 2026-07-17): they are
# NOT a saving over Haiku's $1/$5/$0.10-cached — k2.6 is ~5% cheaper on input
# and 20% on output but 60% dearer on cache reads, and our prompt is cache
# heavy. k3 is worse still at $3/$15. Kept as a working alternative, not a
# cost win. k2.5 is the default nowhere: its pricing is unpublished.
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY", "")
MOONSHOT_BASE_URL = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
MOONSHOT_MODEL = os.getenv("MOONSHOT_MODEL", "kimi-k2.6")
MOONSHOT_INPUT_USD_PER_MTOK = float(os.getenv("MOONSHOT_INPUT_USD_PER_MTOK", "0.95"))
MOONSHOT_CACHED_INPUT_USD_PER_MTOK = float(os.getenv("MOONSHOT_CACHED_INPUT_USD_PER_MTOK", "0.16"))
MOONSHOT_OUTPUT_USD_PER_MTOK = float(os.getenv("MOONSHOT_OUTPUT_USD_PER_MTOK", "4.00"))

# Hedonic model. sklearn is used when available; the repository's NumPy
# fallback keeps training functional in the minimal production venv.
HEDONIC_MODEL_DIR = Path(os.getenv("HEDONIC_MODEL_DIR", str(DATA_DIR / "models"))).expanduser()
HEDONIC_TRAIN_WEEKDAY = int(os.getenv("HEDONIC_TRAIN_WEEKDAY", "0"))
HEDONIC_DEAL_RESIDUAL_PCT = float(os.getenv("HEDONIC_DEAL_RESIDUAL_PCT", "-15"))
HEDONIC_MAX_TRAIN_PRICE_PER_SQM = float(os.getenv("HEDONIC_MAX_TRAIN_PRICE_PER_SQM", "15000"))
DEAL_ENGINE = os.getenv("DEAL_ENGINE", "zscore").strip().lower()

# Authenticity and bait detection.
HASH_MAX_PER_RUN = int(os.getenv("HASH_MAX_PER_RUN", "100"))
HASH_DELAY_SECONDS = float(os.getenv("HASH_DELAY_SECONDS", "1.5"))
IMAGE_HASH_CACHE_PATH = Path(
    os.getenv("IMAGE_HASH_CACHE_PATH", str(DATA_DIR / "cache" / "image_phash.json"))
).expanduser()
AUTHENTICITY_DEAL_MIN_SCORE = int(os.getenv("AUTHENTICITY_DEAL_MIN_SCORE", "50"))
# Bounds the repost/phone-footprint comparison pool to recent inactive
# listings. Without this, score_authenticity() pulled in the ENTIRE
# historical sale corpus (30,433 rows vs 6,386 active) — the 2026-07-13
# nightly hung for 48+ hours hashing and cross-comparing years of sold
# listings before being killed with zero progress committed.
AUTHENTICITY_REPOST_LOOKBACK_DAYS = int(os.getenv("AUTHENTICITY_REPOST_LOOKBACK_DAYS", "90"))
# Defensive cap: if templated/boilerplate text collapses many listings into
# the same near-hash bucket, skip comparing that row rather than let a
# single pathological bucket blow up runtime.
AUTHENTICITY_MAX_HASH_CANDIDATES = int(os.getenv("AUTHENTICITY_MAX_HASH_CANDIDATES", "400"))

# FSBO/general-classified portals. OLX is enabled because its observed Sofia
# JSON API is reachable without Cloudflare workarounds.
OLX_ENABLED = os.getenv("OLX_ENABLED", "1").lower() not in ("0", "false", "no", "")
OLX_MAX_PAGES = int(os.getenv("OLX_MAX_PAGES", "10"))
BAZAR_MAX_PAGES = int(os.getenv("BAZAR_MAX_PAGES", "3"))
ALO_MAX_PAGES = int(os.getenv("ALO_MAX_PAGES", "10"))

# Sanity floor: nothing in Sofia genuinely sells below this — prices under it
# are parse artifacts (a €6 "listing" made Top Pick of the Day, TIN-472).
MIN_PRICE_EUR = float(os.getenv("MIN_PRICE_EUR", "5000"))
MIN_RENT_EUR = float(os.getenv("MIN_RENT_EUR", "100"))
BCPEA_MAX_DETAIL_FETCHES = int(os.getenv("BCPEA_MAX_DETAIL_FETCHES", "20"))
MUNICIPAL_WATCH_WEEKDAY = int(os.getenv("MUNICIPAL_WATCH_WEEKDAY", "0"))
MUNICIPAL_MAX_PAGES = int(os.getenv("MUNICIPAL_MAX_PAGES", "3"))
MUNICIPAL_MAX_NOTICES_PER_RUN = int(os.getenv("MUNICIPAL_MAX_NOTICES_PER_RUN", "20"))
MUNICIPAL_DELAY_SECONDS = float(os.getenv("MUNICIPAL_DELAY_SECONDS", "1.5"))

# Published neighborhood medians below this €/m² are land-dominated blends,
# not residential prices — suppress rather than publish (TIN-476).
PUBLISH_MIN_MEDIAN_EUR_SQM = float(os.getenv("PUBLISH_MIN_MEDIAN_EUR_SQM", "700"))

# Plausible apartment size range. Outside it the "apartment" label is a
# source misclassification (a 7,453 m² "apartment" in Горна баня — really a
# plot — became Top Pick of the Day at €4/m² and polluted the hood's stats).
MIN_APARTMENT_AREA_SQM = float(os.getenv("MIN_APARTMENT_AREA_SQM", "20"))
MAX_APARTMENT_AREA_SQM = float(os.getenv("MAX_APARTMENT_AREA_SQM", "500"))
MIN_APARTMENT_PRICE_PER_SQM_EUR = float(
    os.getenv("MIN_APARTMENT_PRICE_PER_SQM_EUR", "700")
)

# Analysis settings
ANOMALY_ZSCORE_THRESHOLD = float(os.getenv("ANOMALY_ZSCORE_THRESHOLD", "-1.5"))
ANOMALY_PCT_THRESHOLD = float(os.getenv("ANOMALY_PCT_THRESHOLD", "0.85"))
MIN_LISTINGS_PER_GROUP = int(os.getenv("MIN_LISTINGS_PER_GROUP", "5"))
PRICE_DROP_PCT_THRESHOLD = float(os.getenv("PRICE_DROP_PCT_THRESHOLD", "5.0"))
EUR_BGN_RATE = float(os.getenv("EUR_BGN_RATE", "1.9558"))

# Telegram delivery
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Dashboard repo path — where to write data/dashboard/*.json
# during `export-dashboard`. Default assumes both repos sit side-by-side.
# Override via DASHBOARD_REPO_PATH env if your layout differs (e.g. on CI / other dev machine).
DASHBOARD_REPO_PATH = Path(
    os.getenv("DASHBOARD_REPO_PATH", str(BASE_DIR.parent / "sofia-realestate-dashboard"))
).expanduser().resolve()
DASHBOARD_DATA_DIR = DASHBOARD_REPO_PATH / "data" / "dashboard"

# Whether `export-dashboard` should auto git commit + push the regenerated JSON
# files (so Vercel auto-deploys). Set to "0" / "false" to disable.
DASHBOARD_AUTO_PUSH = os.getenv("DASHBOARD_AUTO_PUSH", "1").lower() not in ("0", "false", "no", "")

# User agents for rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# Sofia neighborhoods with zones
SOFIA_NEIGHBORHOODS = {
    # Center
    "Център": "center",
    "Оборище": "center",
    "Докторски паметник": "center",
    "Иван Вазов": "center",
    # South
    "Лозенец": "south",
    "Витоша": "south",
    "Бояна": "south",
    "Драгалевци": "south",
    "Красно село": "south",
    "Манастирски ливади": "south",
    "Кръстова вада": "south",
    "Студентски град": "south",
    "Гоце Делчев": "south",
    "Борово": "south",
    # East
    "Изток": "east",
    "Гео Милев": "east",
    "Яворов": "east",
    "Подуяне": "east",
    "Слатина": "east",
    "Дружба": "east",
    "Младост 1": "east",
    "Младост 2": "east",
    "Младост 3": "east",
    "Младост 4": "east",
    "Младост": "east",
    "Изгрев": "east",
    "Дървеница": "east",
    # North
    "Надежда": "north",
    "Надежда 1": "north",
    "Надежда 2": "north",
    "Надежда 3": "north",
    "Надежда 4": "north",
    "Банишора": "north",
    "Военна рампа": "north",
    "Левски": "north",
    "Орландовци": "north",
    "Требич": "north",
    # West
    "Люлин": "west",
    "Люлин 1": "west",
    "Люлин 2": "west",
    "Люлин 3": "west",
    "Люлин 4": "west",
    "Люлин 5": "west",
    "Люлин 6": "west",
    "Люлин 7": "west",
    "Люлин 8": "west",
    "Люлин 9": "west",
    "Люлин 10": "west",
    "Овча купел": "west",
    "Овча купел 1": "west",
    "Овча купел 2": "west",
    "Горна баня": "west",
    "Красна поляна": "west",
    "Западен парк": "west",
    "Суходол": "west",
    "Фондови жилища": "west",
}
