"""Message sender — real Telegram Bot API delivery.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env (set in ~/.zshrc or .env).

All formatters in src/alerts/telegram.py emit HTML markup (<b>, <a>, <i>), so we
send with parse_mode=HTML. Long messages are auto-truncated to Telegram's 4096-char
limit.
"""

import os
import time
from typing import List, Dict, Any, Optional

import httpx
from loguru import logger

from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

DEFAULT_TELEGRAM_TARGET = TELEGRAM_CHAT_ID

# Telegram API limits
MAX_MESSAGE_LENGTH = 4096
TRUNCATION_SUFFIX = "\n\n<i>… (truncated)</i>"

# Network settings
REQUEST_TIMEOUT = 10.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # seconds, doubled each retry


def _send_message(
    text: str,
    chat_id: str,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = False,
) -> bool:
    """POST a single message to Telegram Bot API with retry on transient failures.

    Returns True on success, False on permanent failure. Logs at WARN/ERROR.
    """
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID must be set to send Telegram messages")

    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN not set — cannot send message. "
            "Export it in ~/.zshrc or .env (see .env.example)."
        )
        return False

    if len(text) > MAX_MESSAGE_LENGTH:
        cut = MAX_MESSAGE_LENGTH - len(TRUNCATION_SUFFIX)
        text = text[:cut] + TRUNCATION_SUFFIX
        logger.warning(f"Message exceeded {MAX_MESSAGE_LENGTH} chars, truncated")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                resp = client.post(url, data=payload)

            if resp.status_code == 200 and resp.json().get("ok"):
                return True

            # 4xx = permanent (bad token, bad chat_id, malformed HTML) — don't retry
            if 400 <= resp.status_code < 500:
                logger.error(
                    f"Telegram rejected message (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}"
                )
                return False

            # 5xx or unexpected — retry
            logger.warning(
                f"Telegram send attempt {attempt}/{MAX_RETRIES} failed "
                f"(HTTP {resp.status_code}): {resp.text[:200]}"
            )

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.warning(
                f"Telegram send attempt {attempt}/{MAX_RETRIES} network error: {e}"
            )
        except Exception as e:  # noqa: BLE001 — last-resort guard for one bad alert
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return False

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))

    logger.error(f"Telegram send permanently failed after {MAX_RETRIES} attempts")
    return False


def send_deal_alerts(
    messages: List[Dict[str, Any]],
    target: str = DEFAULT_TELEGRAM_TARGET,
) -> List[int]:
    """Send deal alerts via Telegram.

    Args:
        messages: list of dicts with keys 'message' (HTML), 'alert_id' (int),
            and optionally 'simple_message'.
        target: Telegram chat ID.

    Returns:
        List of alert_ids that were successfully sent. The caller should mark
        these as sent in the DB.
    """
    if not messages:
        return []

    sent_ids: List[int] = []

    for msg in messages:
        alert_id = msg.get("alert_id")
        message_text = msg.get("message", "")

        if not message_text:
            logger.warning(f"Empty message for alert {alert_id}, skipping")
            continue

        ok = _send_message(message_text, chat_id=target)

        if ok:
            sent_ids.append(alert_id)
            logger.info(f"Sent deal alert {alert_id} to Telegram chat {target}")
            # Telegram rate limit: 30 msgs/sec to different chats, but to a
            # single chat ~1 msg/sec is the safe pace.
            time.sleep(1.0)
        else:
            logger.error(f"Failed to send deal alert {alert_id}")

    return sent_ids


def send_daily_digest(
    new_listings: int,
    price_drops: int,
    underpriced: int,
    avg_price_change: float,
    hot_deals: List[Dict[str, Any]],
    target: str = DEFAULT_TELEGRAM_TARGET,
) -> bool:
    """Send daily digest via Telegram. Returns True on success."""
    from src.alerts.telegram import format_daily_digest

    message = format_daily_digest(
        new_listings=new_listings,
        price_drops=price_drops,
        underpriced=underpriced,
        avg_price_change=avg_price_change,
        hot_deals=hot_deals,
    )

    ok = _send_message(message, chat_id=target)
    if ok:
        logger.info(
            f"Sent daily digest to {target} | "
            f"new={new_listings} deals={underpriced} drops={price_drops}"
        )
    return ok


def send_simple_message(
    text: str,
    target: str = DEFAULT_TELEGRAM_TARGET,
    parse_mode: str = "HTML",
) -> bool:
    """Send a free-form message. Useful for cron status pings and tests."""
    return _send_message(text, chat_id=target, parse_mode=parse_mode)
