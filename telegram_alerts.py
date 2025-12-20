"""
Telegram Alerts Module

Sends push notifications to Telegram when position P&L reaches critical thresholds.

Setup:
1. Create a bot with @BotFather on Telegram
2. Get the bot token
3. Start a chat with your bot and send any message
4. Get your chat ID from: https://api.telegram.org/bot<TOKEN>/getUpdates
5. Set env vars:
   - TELEGRAM_BOT_TOKEN: Your bot token
   - TELEGRAM_CHAT_ID: Your chat ID
   - POSITION_ALERT_THRESHOLDS: Comma-separated thresholds (e.g., 25,35,50)
"""

import logging
import requests
from typing import Dict, Any, Set

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POSITION_ALERT_THRESHOLDS

log = logging.getLogger("telegram")

# Track which alerts have been sent to avoid spam
# Key: "{trade_id}:{threshold}" -> True if already sent
_sent_alerts: Dict[str, bool] = {}


def is_enabled() -> bool:
    """Check if Telegram alerts are configured."""
    return bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_CHAT_ID)


def send_message(text: str) -> bool:
    """Send a message to Telegram. Returns True on success."""
    if not is_enabled():
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.debug(f"Telegram message sent: {text[:50]}...")
            return True
        else:
            log.warning(f"Telegram API error: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        log.warning(f"Failed to send Telegram message: {e}")
        return False


def check_position_alerts(
    trade_id: str,
    symbol: str,
    side: str,
    avg_entry: float,
    current_price: float,
    leverage: int,
    dca_fills: int = 0,
    dca_count: int = 2,
) -> None:
    """
    Check if position P&L has crossed any alert thresholds.
    Sends Telegram notification if a new threshold is crossed.
    """
    if not is_enabled():
        return

    if not avg_entry or not current_price:
        return

    # Calculate position P&L %
    if side == "Sell":  # SHORT
        pnl_pct = (avg_entry - current_price) / avg_entry * 100 * leverage
    else:  # LONG
        pnl_pct = (current_price - avg_entry) / avg_entry * 100 * leverage

    # Check each threshold (negative P&L)
    for threshold in sorted(POSITION_ALERT_THRESHOLDS):
        alert_key = f"{trade_id}:{threshold}"

        # Skip if already alerted for this threshold
        if _sent_alerts.get(alert_key):
            continue

        # Check if threshold crossed (pnl_pct is negative when losing)
        if pnl_pct <= -threshold:
            # Build alert message
            direction = "SHORT" if side == "Sell" else "LONG"
            dca_status = f"DCAs: {dca_fills}/{dca_count}"

            message = (
                f"<b>Position Alert: -{threshold}%</b>\n\n"
                f"<b>{symbol}</b> {direction}\n"
                f"Position P&L: <b>{pnl_pct:.1f}%</b>\n\n"
                f"Avg Entry: ${avg_entry:.6f}\n"
                f"Current: ${current_price:.6f}\n"
                f"{dca_status}"
            )

            if send_message(message):
                _sent_alerts[alert_key] = True
                log.info(f"Sent Telegram alert for {symbol} @ {pnl_pct:.1f}%")


def clear_alerts_for_trade(trade_id: str) -> None:
    """Clear sent alerts when a trade is closed."""
    keys_to_remove = [k for k in _sent_alerts if k.startswith(f"{trade_id}:")]
    for key in keys_to_remove:
        del _sent_alerts[key]


def send_trade_opened(symbol: str, side: str, entry: float, qty: float) -> None:
    """Send notification when a new trade is opened."""
    if not is_enabled():
        return

    direction = "SHORT" if side == "Sell" else "LONG"
    message = (
        f"<b>New Trade Opened</b>\n\n"
        f"<b>{symbol}</b> {direction}\n"
        f"Entry: ${entry:.6f}\n"
        f"Size: {qty}"
    )
    send_message(message)


def send_trade_closed(
    symbol: str,
    side: str,
    pnl: float,
    exit_reason: str,
    tp_fills: int = 0,
    dca_fills: int = 0,
) -> None:
    """Send notification when a trade is closed."""
    if not is_enabled():
        return

    direction = "SHORT" if side == "Sell" else "LONG"
    result = "WIN" if pnl > 0 else "LOSS"
    emoji = "" if pnl > 0 else ""

    message = (
        f"<b>{emoji} Trade Closed: {result}</b>\n\n"
        f"<b>{symbol}</b> {direction}\n"
        f"PnL: <b>${pnl:.4f}</b>\n"
        f"Exit: {exit_reason}\n"
        f"TPs Hit: {tp_fills} | DCAs: {dca_fills}"
    )
    send_message(message)
