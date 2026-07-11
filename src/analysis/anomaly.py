"""Anomaly detection for underpriced listings."""

from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

import numpy as np
from loguru import logger
from sqlalchemy.orm import Session

from src.database.models import Listing
from src.config import ANOMALY_ZSCORE_THRESHOLD, ANOMALY_PCT_THRESHOLD, MIN_LISTINGS_PER_GROUP


@dataclass
class AnomalyResult:
    """Result of anomaly detection."""
    listing: Listing
    zscore: float
    savings_eur: float
    savings_pct: float
    group_mean: float
    group_std: float
    group_count: int


def get_size_bucket(area_sqm: float) -> str:
    """Get size bucket for area."""
    if area_sqm < 45:
        return "studio"
    elif area_sqm < 60:
        return "small_1bed"
    elif area_sqm < 80:
        return "small_2bed"
    elif area_sqm < 100:
        return "large_2bed"
    elif area_sqm < 130:
        return "3bed"
    else:
        return "4bed_plus"


def calculate_neighborhood_stats(
    listings: List[Listing],
    min_group_size: int = None,
) -> Dict[Tuple[str, ...], Dict[str, float]]:
    """Calculate statistics with 3-tier grouping for accuracy.
    
    Tier 1: (neighborhood, property_type, construction_type) — most precise
    Tier 2: (neighborhood, property_type) — medium precision
    Tier 3: (neighborhood, 'all', 'all') — broadest fallback
    
    Construction type matters hugely in Sofia:
    - Brick (тухла): premium, newer or well-maintained
    - Panel (панел): Soviet-era, cheaper, but renovated ones can be decent
    - EPK: edge case
    """
    if min_group_size is None:
        min_group_size = MIN_LISTINGS_PER_GROUP
    
    # Group listings at all 3 tiers
    groups: Dict[Tuple[str, ...], List[float]] = {}
    
    for listing in listings:
        if listing.price_per_sqm_eur <= 0:
            continue
        
        construction = listing.construction_type or 'unknown'
        
        # Tier 1: neighborhood + type + construction (most precise)
        key1 = (listing.neighborhood, listing.property_type, construction)
        groups.setdefault(key1, []).append(listing.price_per_sqm_eur)
        
        # Tier 2: neighborhood + type
        key2 = (listing.neighborhood, listing.property_type, 'all')
        groups.setdefault(key2, []).append(listing.price_per_sqm_eur)
        
        # Tier 3: neighborhood only
        key3 = (listing.neighborhood, 'all', 'all')
        groups.setdefault(key3, []).append(listing.price_per_sqm_eur)
    
    # Calculate statistics
    stats = {}
    for key, prices in groups.items():
        if len(prices) < min_group_size:
            continue
        
        prices_array = np.array(prices)
        stats[key] = {
            'mean': float(np.mean(prices_array)),
            'median': float(np.median(prices_array)),
            'std': float(np.std(prices_array)),
            'p20': float(np.percentile(prices_array, 20)),
            'count': len(prices),
            'min': float(np.min(prices_array)),
            'max': float(np.max(prices_array)),
        }
    
    return stats


def detect_anomalies(
    listings: List[Listing],
    stats: Optional[Dict[Tuple[str, str, str], Dict[str, float]]] = None
) -> List[AnomalyResult]:
    """Detect underpriced listings using Z-score method."""
    
    if stats is None:
        stats = calculate_neighborhood_stats(listings)
    
    anomalies = []
    
    for listing in listings:
        if listing.price_per_sqm_eur <= 0:
            continue
        
        construction = listing.construction_type or 'unknown'
        
        # Try most precise group first, then fall back — but never across
        # property types (TIN-468): the old (hood, 'all', 'all') fallback
        # compared plots/houses against apartment-dominated pools, flagging
        # a €71/m² field as a "−100% deal". A listing with no same-type
        # peers in its neighborhood simply gets no z-score.
        key = (listing.neighborhood, listing.property_type, construction)
        if key not in stats:
            key = (listing.neighborhood, listing.property_type, 'all')
        if key not in stats:
            continue
        
        group_stats = stats[key]
        mean = group_stats['mean']
        std = group_stats['std']
        
        if std == 0:
            continue
        
        # Calculate Z-score
        zscore = (listing.price_per_sqm_eur - mean) / std
        
        # Check if underpriced
        # Condition: Z-score < threshold AND price < pct_threshold * mean
        if zscore < ANOMALY_ZSCORE_THRESHOLD:
            if listing.price_per_sqm_eur < ANOMALY_PCT_THRESHOLD * mean:
                # Calculate savings
                expected_price = mean * listing.area_sqm
                actual_price = listing.price_eur
                savings_eur = expected_price - actual_price
                savings_pct = (savings_eur / expected_price) * 100 if expected_price > 0 else 0
                
                anomalies.append(AnomalyResult(
                    listing=listing,
                    zscore=zscore,
                    savings_eur=savings_eur,
                    savings_pct=savings_pct,
                    group_mean=mean,
                    group_std=std,
                    group_count=group_stats['count'],
                ))
    
    # Sort by savings percentage (best deals first)
    anomalies.sort(key=lambda x: x.savings_pct, reverse=True)
    
    logger.info(f"Detected {len(anomalies)} anomalies from {len(listings)} listings")
    return anomalies


def analyze_database(db: Session) -> List[AnomalyResult]:
    """Analyze all active listings in database."""
    listings = db.query(Listing).filter(
        Listing.is_active == True,
        (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
    ).all()
    logger.info(f"Analyzing {len(listings)} active listings")
    
    if len(listings) < MIN_LISTINGS_PER_GROUP * 3:
        logger.warning(f"Not enough listings for meaningful analysis: {len(listings)}")
        return []
    
    stats = calculate_neighborhood_stats(listings)
    logger.info(f"Calculated stats for {len(stats)} neighborhood groups")
    
    anomalies = detect_anomalies(listings, stats)
    return anomalies
