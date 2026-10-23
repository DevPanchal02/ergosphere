import os
import re
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

# -- logging setup --
log = logging.getLogger("pacer")
log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
log.propagate = False

_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)

if os.getenv("LOG_FILE"):
    try:
        _fh = RotatingFileHandler(os.environ["LOG_FILE"], maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        _fh.setFormatter(_fmt)
        log.addHandler(_fh)
    except OSError as e:
        log.warning("LOG_FILE unusable (%s); logging to stdout only", e)

# -- directories --
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
INCIDENTS_DIR = os.path.join(LOGS_DIR, "incidents")
SPEND_FILE = os.path.join(LOGS_DIR, "spend_limits.json")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(INCIDENTS_DIR, exist_ok=True)

# -- api keys & secrets --
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

def _discover_gemini_keys():
    found = {}
    for k, v in os.environ.items():
        m = re.fullmatch(r"GEMINI_KEY_(\d+)", k)
        if m and v.strip():
            found[int(m.group(1))] = v.strip()
    for i in range(1, 65):
        if i not in found and (val := os.getenv(f"GEMINI_KEY_{i}")):
            if val.strip():
                found[i] = val.strip()
    return [(i, found[i]) for i in sorted(found.keys())]

GEMINI_KEYS = _discover_gemini_keys()

_SECRETS = [s for s in [OPENROUTER_KEY, *[k for _, k in GEMINI_KEYS]] if s]
_KEY_PATTERNS = re.compile(r"AIza[\w-]{30,}|sk-or-[\w-]+")

def scrub(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return _KEY_PATTERNS.sub("***", text)

# -- pricing and routing --
PRICING = {
    "openrouter/xiaomi/mimo-v2.6-pro": (0.435, 0.00435, 0.87),
    "openrouter/openai/gpt-6.1-sol":    (2.00,  0.10,    10.00),
    "gemini/gemini-3.5-flash-lite":    (0.00,  0.00,     0.00),
    "gemini/gemini-3.1-flash-lite":    (0.00,  0.00,     0.00),
}

OPENROUTER_ROUTES = {
    "mimo-v2.6-pro": ("openrouter/xiaomi/mimo-v2.6-pro", None),
    "gpt-6.1-sol-medium": ("openrouter/openai/gpt-6.1-sol", "medium"),
    "gpt-6.1-sol-high":   ("openrouter/openai/gpt-6.1-sol", "high"),
    "gpt-6.1-sol-x-high": ("openrouter/openai/gpt-6.1-sol", "xhigh"),
}

PRIMARY_GEMINI_MODEL = "gemini/gemini-3.5-flash-lite"
FALLBACK_GEMINI_MODEL = "gemini/gemini-3.1-flash-lite"

DEFAULT_DAILY_SPEND_LIMIT_USD = float(os.getenv("DAILY_SPEND_LIMIT_USD", os.getenv("DAILY_BUDGET_USD", "3.00")))
DEFAULT_RUN_SPEND_LIMIT_USD = float(os.getenv("RUN_SPEND_LIMIT_USD", os.getenv("RUN_BUDGET_USD", "1.50")))
RUN_IDLE_SECONDS = float(os.getenv("RUN_IDLE_SECONDS", "900"))
LOCAL_TZ = os.getenv("TIMEZONE", os.getenv("BUDGET_TZ", "America/Toronto"))
MAX_OUT_TOKENS = int(os.getenv("MAX_OUT_TOKENS", "32000"))

GEMINI_RPM_LIMIT = int(os.getenv("GEMINI_KEY_RPM", "15"))
GEMINI_TPM_LIMIT = int(os.getenv("GEMINI_KEY_TPM", "250000"))
GEMINI_RPD_LIMIT = int(os.getenv("GEMINI_KEY_RPD", "500"))
TPM_WINDOW_SECONDS = 60.0

HIGH_COST_THRESHOLD_USD = float(os.getenv("HIGH_COST_THRESHOLD_USD", "0.05"))
HIGH_TOKEN_THRESHOLD = int(os.getenv("HIGH_TOKEN_THRESHOLD", "150000"))

# Timezone resolution with native host fallback on Windows
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(LOCAL_TZ)
    TZ_NOTE = LOCAL_TZ
except Exception:
    # Use Windows system local timezone (EDT / Toronto) so local late nights don't roll into tomorrow
    system_tz = datetime.now().astimezone().tzinfo
    TZ = system_tz if system_tz else timezone.utc
    TZ_NOTE = f"{LOCAL_TZ} (system)"