import os
from dotenv import load_dotenv

load_dotenv()

def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def _get_bool(name: str, default: str = "false") -> bool:
    return _get(name, default).lower() in ("1","true","yes","y","on")

def _get_int(name: str, default: str) -> int:
    return int(_get(name, default))

def _get_float(name: str, default: str) -> float:
    return float(_get(name, default))

# Discord
DISCORD_TOKEN = _get("DISCORD_TOKEN")
CHANNEL_ID    = _get("CHANNEL_ID")

# Bybit
BYBIT_API_KEY    = _get("BYBIT_API_KEY")
BYBIT_API_SECRET = _get("BYBIT_API_SECRET")
BYBIT_TESTNET    = _get_bool("BYBIT_TESTNET","false")
ACCOUNT_TYPE     = _get("ACCOUNT_TYPE","UNIFIED")  # UNIFIED / CONTRACT etc (depends on your Bybit account)

RECV_WINDOW = _get("RECV_WINDOW","5000")

# Trading
CATEGORY = _get("CATEGORY","linear")   # linear for USDT perpetual
QUOTE    = _get("QUOTE","USDT").upper()

LEVERAGE = _get_int("LEVERAGE","5")
RISK_PCT = _get_float("RISK_PCT","5")

# Limits / Safety
MAX_CONCURRENT_TRADES = _get_int("MAX_CONCURRENT_TRADES","3")
MAX_TRADES_PER_DAY    = _get_int("MAX_TRADES_PER_DAY","20")
TC_MAX_LAG_SEC        = _get_int("TC_MAX_LAG_SEC","300")

# Entry rules
ENTRY_EXPIRATION_MIN         = _get_int("ENTRY_EXPIRATION_MIN","180")
ENTRY_TOO_FAR_PCT            = _get_float("ENTRY_TOO_FAR_PCT","0.5")
ENTRY_TRIGGER_BUFFER_PCT     = _get_float("ENTRY_TRIGGER_BUFFER_PCT","0.0")
ENTRY_LIMIT_PRICE_OFFSET_PCT = _get_float("ENTRY_LIMIT_PRICE_OFFSET_PCT","0.0")
ENTRY_EXPIRATION_PRICE_PCT   = _get_float("ENTRY_EXPIRATION_PRICE_PCT","0.6")

# TP/SL
MOVE_SL_TO_BE_ON_TP1 = _get_bool("MOVE_SL_TO_BE_ON_TP1","true")
INITIAL_SL_PCT = _get_float("INITIAL_SL_PCT","19.0")  # SL distance from entry in %

TP_SPLITS = [float(x) for x in _get("TP_SPLITS","30,30,30,10").split(",") if x.strip()]
if abs(sum(TP_SPLITS) - 100.0) > 0.001:
    # keep it safe: normalize to 100
    s = sum(TP_SPLITS) or 100.0
    TP_SPLITS = [x * 100.0 / s for x in TP_SPLITS]

# Fallback TP distances (% from entry) if signal has no TPs
FALLBACK_TP_PCT = [float(x) for x in _get("FALLBACK_TP_PCT","0.85,1.65,4.0").split(",") if x.strip()]

TRAIL_AFTER_TP_INDEX = _get_int("TRAIL_AFTER_TP_INDEX","3")  # start trailing when TPn filled
TRAIL_DISTANCE_PCT   = _get_float("TRAIL_DISTANCE_PCT","2.0")
TRAIL_ACTIVATE_ON_TP = _get_bool("TRAIL_ACTIVATE_ON_TP","true")

# DCA sizing multipliers vs BASE qty
DCA_QTY_MULTS = [float(x) for x in _get("DCA_QTY_MULTS","1.5,2.25,3.0").split(",") if x.strip()]

# Timing
POLL_SECONDS    = _get_int("POLL_SECONDS","15")
POLL_JITTER_MAX = _get_int("POLL_JITTER_MAX","5")

# Misc
DRY_RUN     = _get_bool("DRY_RUN","true")
STATE_FILE  = _get("STATE_FILE","state.json")
LOG_LEVEL   = _get("LOG_LEVEL","INFO").upper()
