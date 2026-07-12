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
MARK_INACTIVE_MIN_RATIO = float(os.getenv("MARK_INACTIVE_MIN_RATIO", "0.5"))
SOLD_AFTER_DAYS = int(os.getenv("SOLD_AFTER_DAYS", "14"))

# Sanity floor: nothing in Sofia genuinely sells below this — prices under it
# are parse artifacts (a €6 "listing" made Top Pick of the Day, TIN-472).
MIN_PRICE_EUR = float(os.getenv("MIN_PRICE_EUR", "5000"))

# Published neighborhood medians below this €/m² are land-dominated blends,
# not residential prices — suppress rather than publish (TIN-476).
PUBLISH_MIN_MEDIAN_EUR_SQM = float(os.getenv("PUBLISH_MIN_MEDIAN_EUR_SQM", "700"))

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
