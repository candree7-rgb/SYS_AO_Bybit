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

# Exchange — Binance USDT-M Futures.
# Legacy BYBIT_* env vars are accepted as fallbacks so an existing
# Railway deploy keeps booting without a config edit.
BINANCE_API_KEY    = _get("BINANCE_API_KEY") or _get("BYBIT_API_KEY")
BINANCE_API_SECRET = _get("BINANCE_API_SECRET") or _get("BYBIT_API_SECRET")
BINANCE_TESTNET    = _get_bool("BINANCE_TESTNET", "false") or _get_bool("BYBIT_TESTNET", "false")

# Margin mode applied per traded symbol on first leverage-set call.
# ISOLATED caps loss to that position's margin (recommended).
MARGIN_MODE = _get("MARGIN_MODE", "ISOLATED").upper()  # ISOLATED | CROSSED

# Account-type kept for back-compat with trade_engine.py — passed through
# to wallet_equity() which translates UNIFIED → USDT for Binance.
ACCOUNT_TYPE = _get("ACCOUNT_TYPE", "USDT")

# Bot identification (for multi-bot dashboard support)
BOT_ID = _get("BOT_ID", "ao")  # Unique identifier for this bot instance

RECV_WINDOW = _get("RECV_WINDOW","5000")

# Trading
CATEGORY = _get("CATEGORY","linear")   # linear for USDT perpetual
QUOTE    = _get("QUOTE","USDT").upper()

LEVERAGE = _get_int("LEVERAGE","5")
RISK_PCT = _get_float("RISK_PCT","5")

# Per-symbol leverage overrides for coins that don't support our default
# leverage. Format: "SIREN:5,DOGE:10" (base symbol → max leverage).
# When matched, the bot:
#   - calls set_leverage with the override value (avoids Bybit error)
#   - scales risk_pct UP so notional stays constant (i.e. same position
#     size and same dollar-risk per trade as default leverage trades).
# Example: default 5%/20x = 100% notional. SIREN:5 → 20%/5x = 100% notional.
LEVERAGE_OVERRIDES: dict = {}
_lev_override_str = _get("LEVERAGE_OVERRIDES", "")
if _lev_override_str:
    for _pair in _lev_override_str.split(","):
        _pair = _pair.strip()
        if ":" in _pair:
            _sym, _lev = _pair.split(":", 1)
            try:
                # float() so 12.5 works (e.g. B, FHE, HIGH on Bybit)
                _val = float(_lev.strip())
                LEVERAGE_OVERRIDES[_sym.strip().upper()] = int(_val) if _val.is_integer() else _val
            except ValueError:
                pass

# Symbols to pre-warm Bybit caches for at bot start. For each listed symbol
# the bot fetches instrument-info AND issues set_leverage in parallel before
# the first signal arrives — eliminates the ~300ms cold-path penalty on
# the first trade per symbol. Format: comma-separated base symbols,
# e.g. "BTC,ETH,SOL,SIREN,UB,B,FHE,HIGH".
WARMUP_SYMBOLS = [s.strip().upper() for s in _get("WARMUP_SYMBOLS", "").split(",") if s.strip()]

# Blacklisted symbols — incoming signals on these are SKIPPED entirely.
# Use for symbols with proven negative EV in backtest (e.g. HIGH had
# −3.2% EV/trade across 16 historical trades).
# Format: comma-separated base symbols, e.g. "HIGH,BLESS".
BLACKLIST_SYMBOLS = set(s.strip().upper() for s in _get("BLACKLIST_SYMBOLS", "").split(",") if s.strip())

# Fixed risk profile — overrides any SL/TPs from the signal with hardcoded
# percentages. The AO Crusher provider is consistent (TP1=0.8% TP2=1.6%
# TP3=4% SL=1% on scalp signals). Setting this to true makes the bot
# ignore the signal's SL/TPs entirely and use these fixed values, which:
#   - keeps risk fixed regardless of swing/scalp signal variants
#   - makes the bot robust against signal format changes
#   - minimal speed gain (parsing is sub-ms either way)
FIXED_RISK_PROFILE = _get_bool("FIXED_RISK_PROFILE","true")
# Tick-precise backtest (1013 signals, all Binance aggTrades) shows
# splits=[10,30,60] is the OPTIMAL config. The 22% of signals that
# reach TP3 monotonically (no upward retrace to BE level) give +58%
# margin per trade with the 60% TP3 piece — that's where the EV comes
# from. With splits=[0,100,0] those big wins get capped at TP2.
# Top config: SL=2.0%, splits=[10,30,60], BE+0.7% → +9.56% EV/sig,
# 28% MaxDD, 59% WR. See event_sweep.py for the full sweep result.
FIXED_SL_PCT       = _get_float("FIXED_SL_PCT","2.0")
FIXED_TP_PCTS      = [float(x) for x in _get("FIXED_TP_PCTS","0.8,1.6,4.0").split(",") if x.strip()]

# Limits / Safety
MAX_CONCURRENT_TRADES = _get_int("MAX_CONCURRENT_TRADES","3")
MAX_TRADES_PER_DAY    = _get_int("MAX_TRADES_PER_DAY","20")
TC_MAX_LAG_SEC        = _get_int("TC_MAX_LAG_SEC","300")

# Entry rules
ENTRY_EXPIRATION_MIN         = _get_int("ENTRY_EXPIRATION_MIN","180")
# Skip entry if market is already further past trigger than TP1 — at that
# point the trade has no upside left. Default 0.8% matches FIXED_TP_PCTS[0].
ENTRY_TOO_FAR_PCT            = _get_float("ENTRY_TOO_FAR_PCT","0.8")
ENTRY_TRIGGER_BUFFER_PCT     = _get_float("ENTRY_TRIGGER_BUFFER_PCT","0.0")
ENTRY_LIMIT_PRICE_OFFSET_PCT = _get_float("ENTRY_LIMIT_PRICE_OFFSET_PCT","0.0")
ENTRY_EXPIRATION_PRICE_PCT   = _get_float("ENTRY_EXPIRATION_PRICE_PCT","0.6")

# TP/SL
MOVE_SL_TO_BE_ON_TP1 = _get_bool("MOVE_SL_TO_BE_ON_TP1","true")
# BE+buffer: after TP1 SL is moved to entry × (1 ± buffer%). Constraint:
# buffer MUST be < TP1 distance (0.8%) — otherwise the BE level lands
# on the wrong side of current price and Binance rejects with -2021.
# Sweep winner: 0.7% (just below TP1) → locks +0.7% on retracements,
# leaves enough room for the TP3 runner trades to develop.
BREAKEVEN_PROFIT_BUFFER_PCT = _get_float("BREAKEVEN_PROFIT_BUFFER_PCT","0.7")
INITIAL_SL_PCT = _get_float("INITIAL_SL_PCT","19.0")  # SL distance from entry in %

# TP_SPLITS: percentage of position to close at each TP level.
# Tick-precise sweep winner: 10,30,60. The 60% TP3 piece captures the
# big monotonic moves (~22% of signals) at +4% × 60% × 20x = +48%
# margin per such trade — that's where the +9.56% EV comes from.
# DO NOT normalize - allow sum < 100% for runner positions
TP_SPLITS = [float(x) for x in _get("TP_SPLITS","10,30,60").split(",") if x.strip()]
if sum(TP_SPLITS) > 100.0:
    # Only normalize if over 100% (user error)
    s = sum(TP_SPLITS)
    TP_SPLITS = [x * 100.0 / s for x in TP_SPLITS]

# Fallback TP distances (% from entry) if signal has no TPs
FALLBACK_TP_PCT = [float(x) for x in _get("FALLBACK_TP_PCT","0.85,1.65,4.0").split(",") if x.strip()]

TRAIL_AFTER_TP_INDEX = _get_int("TRAIL_AFTER_TP_INDEX","3")  # start trailing when TPn filled
TRAIL_DISTANCE_PCT   = _get_float("TRAIL_DISTANCE_PCT","2.0")
TRAIL_ACTIVATE_ON_TP = _get_bool("TRAIL_ACTIVATE_ON_TP","true")

# DCA sizing multipliers vs BASE qty
# Example: 1.5,2.25 means DCA1 = 1.5x base qty, DCA2 = 2.25x base qty
# Only places as many DCAs as there are multipliers (ignores extra DCA prices from signal)
DCA_QTY_MULTS = [float(x) for x in _get("DCA_QTY_MULTS","1.5,2.25").split(",") if x.strip()]

# Timing
POLL_SECONDS    = _get_int("POLL_SECONDS","15")
POLL_JITTER_MAX = _get_int("POLL_JITTER_MAX","5")
SIGNAL_UPDATE_INTERVAL_SEC = _get_int("SIGNAL_UPDATE_INTERVAL_SEC","60")  # Check for signal updates every 60s (pending trades)
SIGNAL_UPDATE_INTERVAL_OPEN_SEC = _get_int("SIGNAL_UPDATE_INTERVAL_OPEN_SEC","60")  # Check every 60s for open trades (only TRADE CLOSED/CANCELLED detection)

# Discord Gateway WebSocket (push instead of REST polling for new messages).
# When enabled, new-message detection happens via Gateway WS (~50-300ms push
# latency) instead of REST polling (POLL_SECONDS). Edit polling above stays on
# REST for TRADE CLOSED/CANCELLED detection.
USE_GATEWAY_WS              = _get_bool("USE_GATEWAY_WS","true")
GATEWAY_FALLBACK_FAILURES   = _get_int("GATEWAY_FALLBACK_FAILURES","3")  # after N consecutive connect failures, fall back to REST polling
GATEWAY_LOOP_SLEEP_SEC      = _get_float("GATEWAY_LOOP_SLEEP_SEC","0.5") # main-loop sleep when WS is healthy (drain-only mode)
GATEWAY_INITIAL_BACKFILL    = _get_bool("GATEWAY_INITIAL_BACKFILL","true") # one REST fetch on startup so messages during downtime aren't lost

# Misc
DRY_RUN     = _get_bool("DRY_RUN","true")
STATE_FILE  = _get("STATE_FILE","state.json")
LOG_LEVEL   = _get("LOG_LEVEL","INFO").upper()

# Telegram Alerts
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _get("TELEGRAM_CHAT_ID")
# Position P&L thresholds to trigger alerts (e.g., 25,35,50 = alert at -25%, -35%, -50%)
POSITION_ALERT_THRESHOLDS = [float(x) for x in _get("POSITION_ALERT_THRESHOLDS", "25,35,50").split(",") if x.strip()]
