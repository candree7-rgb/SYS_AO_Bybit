import os
from dotenv import load_dotenv

load_dotenv()

def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1","true","yes","y","on")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN","").strip()
CHANNEL_ID    = os.getenv("CHANNEL_ID","").strip()

BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY","").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET","").strip()
BYBIT_TESTNET    = _bool(os.getenv("BYBIT_TESTNET","false"))

CATEGORY = os.getenv("CATEGORY","linear").strip()
QUOTE    = os.getenv("QUOTE","USDT").strip().upper()

DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE","5"))

MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES","3"))
MAX_TRADES_PER_DAY    = int(os.getenv("MAX_TRADES_PER_DAY","20"))

ENTRY_EXPIRATION_MIN       = int(os.getenv("ENTRY_EXPIRATION_MIN","180"))
ENTRY_TOO_FAR_PCT          = float(os.getenv("ENTRY_TOO_FAR_PCT","0.5"))
ENTRY_TRIGGER_BUFFER_PCT   = float(os.getenv("ENTRY_TRIGGER_BUFFER_PCT","0.0"))
ENTRY_LIMIT_PRICE_OFFSET_PCT = float(os.getenv("ENTRY_LIMIT_PRICE_OFFSET_PCT","0.0"))

INITIAL_SL_PCT = float(os.getenv("INITIAL_SL_PCT","19.0"))
MOVE_SL_TO_BE_ON_TP1 = _bool(os.getenv("MOVE_SL_TO_BE_ON_TP1","true"))

TP_SPLITS = [float(x.strip()) for x in os.getenv("TP_SPLITS","30,30,30,10").split(",") if x.strip()]
DCA_QTY_MULTS = [float(x.strip()) for x in os.getenv("DCA_QTY_MULTS","1.0,1.0,1.0").split(",") if x.strip()]

POLL_SECONDS = int(os.getenv("POLL_SECONDS","15"))
DRY_RUN = _bool(os.getenv("DRY_RUN","false"))
