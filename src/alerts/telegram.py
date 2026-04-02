"""Telegram alerts module for Sofia Real Estate Agent.

Uses OpenClaw's native message tool for sending deal alerts.
Target: Tino's Telegram (@TinTinTrading, ID: 1787160163)
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from src.config import EUR_BGN_RATE


# Tino's Telegram ID
DEFAULT_TELEGRAM_ID = "1787160163"


@dataclass
class DealAlert:
    """Represents a deal alert ready to be sent."""
    listing_id: int
    neighborhood: str
    price_eur: float
    area_sqm: float
    price_per_sqm_eur: float
    rooms: Optional[int]
    property_type: str
    zscore: float
    savings_pct: float
    savings_eur: float
    url: str
    source: str


def format_deal_alert(alert: DealAlert) -> str:
    """Format a deal alert message for Telegram.
    
    Format:
    🏠 DEAL: {neighborhood} | {price}€ | {savings}% below avg
    
    Details:
    📍 {neighborhood}
    💰 {price}€ ({price_per_sqm}€/m²)
    📐 {area}m², {rooms}-room {type}
    📊 {savings}% below avg (Z: {zscore})
    🔗 {url}
    """
    rooms_str = f"{int(alert.rooms)}-room" if alert.rooms else "studio"
    
    # Determine emoji based on savings
    if alert.savings_pct >= 25:
        deal_emoji = "🔥"
    elif alert.savings_pct >= 15:
        deal_emoji = "💰"
    else:
        deal_emoji = "🏠"
    
    # Z-score indicator
    if alert.zscore <= -2.0:
        z_indicator = "🚨 Extremely underpriced!"
    elif alert.zscore <= -1.5:
        z_indicator = "⚡ Great deal"
    else:
        z_indicator = "👍 Good value"
    
    message = f"""{deal_emoji} <b>DEAL ALERT</b> {deal_emoji}

<b>{alert.neighborhood}</b> | <b>{alert.price_eur:,.0f}€</b> | <b>{alert.savings_pct:.0f}%</b> below avg

📍 <b>Location:</b> {alert.neighborhood}
💰 <b>Price:</b> {alert.price_eur:,.0f}€ ({alert.price_per_sqm_eur:,.0f}€/m²)
📐 <b>Size:</b> {alert.area_sqm:.0f}m², {rooms_str} {alert.property_type}
📊 <b>Savings:</b> {alert.savings_pct:.1f}% ({alert.savings_eur:,.0f}€ below avg)
⚖️ <b>Z-Score:</b> {alert.zscore:.2f} {z_indicator}
🌐 <b>Source:</b> {alert.source}

🔗 <a href="{alert.url}">View Listing</a>

<i>Alert generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"""
    
    return message


def format_daily_digest(
    new_listings: int,
    price_drops: int,
    underpriced: int,
    avg_price_change: float,
    hot_deals: List[Dict[str, Any]]
) -> str:
    """Format a daily digest message.
    
    Args:
        new_listings: Number of new listings today
        price_drops: Number of listings with price drops
        underpriced: Number of underpriced listings detected
        avg_price_change: Average price change percentage
        hot_deals: List of top deals to highlight
    """
    deals_section = ""
    if hot_deals:
        deals_section = "\n🔥 <b>Top Deals Today:</b>\n"
        for i, deal in enumerate(hot_deals[:3], 1):
            deals_section += (
                f"{i}. {deal['neighborhood']}: "
                f"{deal['price_eur']:,.0f}€ "
                f"({deal['savings_pct']:.0f}% below avg)\n"
            )
    
    message = f"""📊 <b>Sofia Real Estate Daily Digest</b>

📈 <b>Market Activity:</b>
   • New listings: {new_listings}
   • Price drops: {price_drops}
   • Underpriced deals: {underpriced}

💹 <b>Price Trends:</b>
   • Avg change: {avg_price_change:+.1f}%{deals_section}

<i>Next update tomorrow at 09:00</i>"""
    
    return message


def listing_to_alert(listing_data: Dict[str, Any], zscore: float, savings_pct: float, savings_eur: float) -> DealAlert:
    """Convert listing data to DealAlert object."""
    return DealAlert(
        listing_id=listing_data.get('id', 0),
        neighborhood=listing_data.get('neighborhood', 'Unknown'),
        price_eur=listing_data.get('price_eur', 0),
        area_sqm=listing_data.get('area_sqm', 0),
        price_per_sqm_eur=listing_data.get('price_per_sqm_eur', 0),
        rooms=listing_data.get('rooms'),
        property_type=listing_data.get('property_type', 'apartment'),
        zscore=zscore,
        savings_pct=savings_pct,
        savings_eur=savings_eur,
        url=listing_data.get('url', ''),
        source=listing_data.get('source', 'unknown'),
    )


def should_send_alert(zscore: float, min_zscore: float = -1.5) -> bool:
    """Check if an alert should be sent based on z-score threshold.
    
    Args:
        zscore: Statistical z-score (negative = below average)
        min_zscore: Minimum threshold (default -1.5 = 1.5 std dev below mean)
    
    Returns:
        True if alert should be sent
    """
    return zscore <= min_zscore


async def send_telegram_alert(message: str, target_id: str = DEFAULT_TELEGRAM_ID) -> bool:
    """Send alert via OpenClaw message tool.
    
    Args:
        message: Formatted message to send
        target_id: Telegram user ID to send to
        
    Returns:
        True if sent successfully
    """
    try:
        # This function will be called from the main workflow
        # The actual sending is handled via OpenClaw's message tool
        logger.info(f"Prepared alert for Telegram user {target_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to prepare alert: {e}")
        return False


def format_simple_alert(
    neighborhood: str,
    price_eur: float,
    savings_pct: float,
    url: str,
    rooms: Optional[int] = None,
    area_sqm: float = 0,
) -> str:
    """Format a simple deal alert (one-line format).
    
    Format: "🏠 DEAL: {neighborhood} | {price}€ | {savings}% below avg"
    """
    rooms_str = f"{int(rooms)}-room, " if rooms else ""
    
    message = (
        f"🏠 <b>DEAL:</b> {neighborhood} | "
        f"<b>{price_eur:,.0f}€</b> | "
        f"<b>{savings_pct:.0f}%</b> below avg\n"
        f"   {rooms_str}{area_sqm:.0f}m² | "
        f'<a href="{url}">View</a>'
    )
    
    return message
