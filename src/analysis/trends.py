"""Trend analysis for neighborhood prices."""

from typing import List, Dict, Any, Optional
from datetime import timedelta
from collections import defaultdict

from sqlalchemy.orm import Session
from sqlalchemy import func
from loguru import logger

from src.database.models import Listing, PriceHistory, Neighborhood
from src.utils.time import utc_now


def calculate_neighborhood_trends(db: Session, days: int = 30) -> Dict[str, Any]:
    """Calculate price trends per neighborhood over time."""
    
    # Get current prices by neighborhood
    cutoff_date = utc_now() - timedelta(days=days)
    
    # Query active listings by neighborhood
    results = db.query(
        Listing.neighborhood,
        func.avg(Listing.price_per_sqm_eur).label('avg_price'),
        func.count(Listing.id).label('count')
    ).filter(
        Listing.is_active == True
    ).group_by(Listing.neighborhood).all()
    
    trends = {}
    for neighborhood, avg_price, count in results:
        trends[neighborhood] = {
            'current_avg': float(avg_price),
            'listing_count': count,
            'trend': 'stable',
        }
    
    # Compare with historical data
    historical = db.query(
        PriceHistory,
        Listing.neighborhood
    ).join(Listing).filter(
        PriceHistory.recorded_at < cutoff_date
    ).all()
    
    # Group historical by neighborhood
    hist_by_neighborhood = defaultdict(list)
    for ph, neighborhood in historical:
        hist_by_neighborhood[neighborhood].append(ph.price_per_sqm_eur)
    
    # Calculate trends
    for neighborhood, current_data in trends.items():
        if neighborhood in hist_by_neighborhood:
            hist_prices = hist_by_neighborhood[neighborhood]
            if hist_prices:
                hist_avg = sum(hist_prices) / len(hist_prices)
                current_avg = current_data['current_avg']
                
                pct_change = ((current_avg - hist_avg) / hist_avg) * 100
                
                if pct_change > 5:
                    current_data['trend'] = 'up'
                elif pct_change < -5:
                    current_data['trend'] = 'down'
                else:
                    current_data['trend'] = 'stable'
                
                current_data['pct_change_30d'] = round(pct_change, 2)
                current_data['previous_avg'] = round(hist_avg, 2)
    
    return trends


def get_price_history(db: Session, listing_id: int) -> List[Dict[str, Any]]:
    """Get price history for a specific listing."""
    history = db.query(PriceHistory).filter(
        PriceHistory.listing_id == listing_id
    ).order_by(PriceHistory.recorded_at).all()
    
    return [
        {
            'price_eur': h.price_eur,
            'price_per_sqm_eur': h.price_per_sqm_eur,
            'recorded_at': h.recorded_at.isoformat(),
        }
        for h in history
    ]


def detect_price_drops(db: Session, days: int = 7) -> List[Dict[str, Any]]:
    """Detect listings with recent price drops."""
    cutoff_date = utc_now() - timedelta(days=days)
    
    # Get all price history entries in the period
    history = db.query(PriceHistory, Listing).join(Listing).filter(
        PriceHistory.recorded_at >= cutoff_date
    ).all()
    
    # Group by listing
    by_listing = defaultdict(list)
    for ph, listing in history:
        by_listing[listing.id].append((ph, listing))
    
    drops = []
    for listing_id, entries in by_listing.items():
        if len(entries) < 2:
            continue
        
        # Sort by date
        entries.sort(key=lambda x: x[0].recorded_at)
        
        first_price = entries[0][0].price_eur
        last_price = entries[-1][0].price_eur
        
        if last_price < first_price:
            drop_pct = ((first_price - last_price) / first_price) * 100
            if drop_pct >= 5:  # At least 5% drop
                drops.append({
                    'listing': entries[-1][1],
                    'original_price': first_price,
                    'current_price': last_price,
                    'drop_eur': first_price - last_price,
                    'drop_pct': round(drop_pct, 2),
                })
    
    # Sort by drop percentage
    drops.sort(key=lambda x: x['drop_pct'], reverse=True)
    return drops


def generate_market_summary(db: Session) -> Dict[str, Any]:
    """Generate market summary statistics."""
    
    # Total listings
    total = db.query(Listing).filter(Listing.is_active == True).count()
    
    # Average price per sqm
    avg_price = db.query(func.avg(Listing.price_per_sqm_eur)).filter(
        Listing.is_active == True
    ).scalar()
    
    # By property type
    by_type = db.query(
        Listing.property_type,
        func.avg(Listing.price_per_sqm_eur),
        func.count(Listing.id)
    ).filter(Listing.is_active == True).group_by(Listing.property_type).all()
    
    # By zone (if neighborhoods have zones assigned)
    neighborhoods = db.query(Neighborhood).all()
    zone_stats = defaultdict(lambda: {'count': 0, 'total_price': 0})
    
    for n in neighborhoods:
        if n.avg_price_per_sqm and n.zone:
            zone_stats[n.zone]['count'] += n.listing_count
            zone_stats[n.zone]['total_price'] += n.avg_price_per_sqm * n.listing_count
    
    zone_averages = {}
    for zone, data in zone_stats.items():
        if data['count'] > 0:
            zone_averages[zone] = round(data['total_price'] / data['count'], 2)
    
    return {
        'total_listings': total,
        'avg_price_per_sqm': round(avg_price, 2) if avg_price else 0,
        'by_property_type': {
            pt: {'avg_price': round(avg, 2), 'count': count}
            for pt, avg, count in by_type
        },
        'by_zone': zone_averages,
        'generated_at': utc_now().isoformat(),
    }
