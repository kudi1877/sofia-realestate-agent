"""Message sender module for OpenClaw integration.

This module provides functions to send messages via OpenClaw's native
tools. It handles sending deal alerts to Telegram.
"""

import json
from typing import List, Dict, Any
from loguru import logger

# Default target: Tino's Telegram
DEFAULT_TELEGRAM_TARGET = "1787160163"


def send_deal_alerts(messages: List[Dict[str, Any]], target: str = DEFAULT_TELEGRAM_TARGET) -> int:
    """Send deal alerts via OpenClaw message tool.
    
    This function prepares messages for sending. In the OpenClaw environment,
    the actual sending is handled by the message tool.
    
    Args:
        messages: List of message dicts with 'message', 'alert_id', 'simple_message'
        target: Telegram user ID to send to
        
    Returns:
        Number of messages sent
    """
    if not messages:
        return 0
    
    sent_count = 0
    
    for msg in messages:
        try:
            # Use the detailed message for Telegram
            message_text = msg.get('message', '')
            
            if not message_text:
                logger.warning(f"Empty message for alert {msg.get('alert_id')}, skipping")
                continue
            
            # Log the message for OpenClaw to pick up
            # In the actual environment, this would use the message tool
            logger.info(f"[TELEGRAM_ALERT] To: {target} | Alert ID: {msg.get('alert_id')}")
            logger.info(f"[TELEGRAM_MESSAGE] {msg.get('simple_message', '')}")
            
            # Print message in a format that can be captured
            print(f"\n{'='*60}")
            print(f"TELEGRAM ALERT (to: {target})")
            print(f"{'='*60}")
            print(message_text)
            print(f"{'='*60}\n")
            
            sent_count += 1
            
        except Exception as e:
            logger.error(f"Error sending alert {msg.get('alert_id')}: {e}")
    
    return sent_count


def send_daily_digest(
    new_listings: int,
    price_drops: int,
    underpriced: int,
    avg_price_change: float,
    hot_deals: List[Dict[str, Any]],
    target: str = DEFAULT_TELEGRAM_TARGET
) -> bool:
    """Send daily digest via Telegram.
    
    Args:
        new_listings: Number of new listings today
        price_drops: Number of listings with price drops
        underpriced: Number of underpriced listings detected
        avg_price_change: Average price change percentage
        hot_deals: List of top deals to highlight
        target: Telegram user ID
        
    Returns:
        True if sent successfully
    """
    from src.alerts.telegram import format_daily_digest
    
    message = format_daily_digest(
        new_listings=new_listings,
        price_drops=price_drops,
        underpriced=underpriced,
        avg_price_change=avg_price_change,
        hot_deals=hot_deals,
    )
    
    print(f"\n{'='*60}")
    print(f"DAILY DIGEST (to: {target})")
    print(f"{'='*60}")
    print(message)
    print(f"{'='*60}\n")
    
    logger.info(f"[DAILY_DIGEST] To: {target} | New: {new_listings}, Deals: {underpriced}")
    
    return True


def send_simple_message(text: str, target: str = DEFAULT_TELEGRAM_TARGET) -> bool:
    """Send a simple text message.
    
    Args:
        text: Message text
        target: Telegram user ID
        
    Returns:
        True if sent successfully
    """
    print(f"\n[SIMPLE_MESSAGE to {target}]: {text}\n")
    logger.info(f"[SIMPLE_MESSAGE] To: {target} | Text: {text[:50]}...")
    return True
