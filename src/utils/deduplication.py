"""Cross-source deduplication module for Sofia Real Estate Agent.

This module provides functionality to identify and merge duplicate listings
across different sources based on property fingerprints.

Fingerprint format: {neighborhood}_{rounded_area}_{rooms}_{price_range}_{listing_kind}
Source priority: imoti.info > imot.bg > others
"""

import hashlib
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict

from loguru import logger


# Source priority for keeping "best" listing (lower number = higher priority)
SOURCE_PRIORITY = {
    'imotiinfo': 1,   # Highest quality data
    'imotbg': 2,      # Good coverage
    'homesbg': 3,     # Medium quality
    'imotinet': 4,    # Lower coverage
    'propertybg': 5,  # Lowest priority
    'imotbg-rent': 2,
    'homesbg-rent': 3,
    'olx': 6,
    'bazar': 7,
    'alo': 8,
    'bcpea': 9,
    'municipal': 10,
}


def normalize_neighborhood(neighborhood: str) -> str:
    """Normalize neighborhood name for consistent fingerprinting."""
    if not neighborhood:
        return 'unknown'
    
    # Convert to lowercase
    normalized = neighborhood.lower()
    
    # Remove common prefixes
    normalized = normalized.replace('ж.к.', '').replace('жк ', '')
    normalized = normalized.replace('кв. ', '').replace('кв ', '')
    normalized = normalized.replace('м-т ', '').replace('м-т. ', '')
    
    # Remove common suffixes
    normalized = normalized.replace(' district', '').replace(' район', '')
    
    # Clean up whitespace
    normalized = normalized.strip().replace('  ', ' ')
    
    return normalized


def round_area(area_sqm: float) -> int:
    """Round area to nearest 5 sqm for grouping similar properties."""
    if not area_sqm or area_sqm <= 0:
        return 0
    return round(area_sqm / 5) * 5


def get_price_range(price_eur: float) -> str:
    """Get price range bucket for fingerprinting.
    
    Uses 5% bands to group similar prices while avoiding
    false positives from minor price adjustments.
    """
    if not price_eur or price_eur <= 0:
        return 'unknown'
    
    # Round to nearest 5% band
    # e.g., €100,000-€104,999 → '100000', €105,000-€109,999 → '105000'
    band_size = max(5000, round(price_eur * 0.05 / 5000) * 5000)
    band = round(price_eur / band_size) * band_size
    return str(int(band))


def generate_fingerprint(listing: Dict[str, Any]) -> str:
    """Generate a fingerprint for deduplication.
    
    Format: {normalized_neighborhood}_{rounded_area}_{rooms}_{price_range}
    
    Args:
        listing: Dictionary with neighborhood, area_sqm, rooms, price_eur
        
    Returns:
        Fingerprint string
    """
    neighborhood = normalize_neighborhood(listing.get('neighborhood', ''))
    area = round_area(listing.get('area_sqm', 0))
    rooms = listing.get('rooms') or 0
    price_range = get_price_range(listing.get('price_eur', 0))
    
    # Handle missing rooms by using property type
    if rooms == 0:
        prop_type = listing.get('property_type', 'unknown')
        if prop_type == 'studio':
            rooms = 1
        elif prop_type == 'house':
            rooms = 0  # Houses vary more, use 0 as wildcard
    
    listing_kind = listing.get("listing_kind") or "sale"
    fingerprint = f"{neighborhood}_{area}_{rooms}_{price_range}_{listing_kind}"
    return fingerprint


def generate_canonical_id(fingerprint: str) -> str:
    """Generate a canonical ID from fingerprint using hash.
    
    This provides a stable, unique identifier for a property
    regardless of which source it comes from.
    """
    return hashlib.md5(fingerprint.encode()).hexdigest()[:16]


def get_source_priority(source: str) -> int:
    """Get priority ranking for a source."""
    return SOURCE_PRIORITY.get(source, 99)


def should_replace_existing(existing: Dict[str, Any], new: Dict[str, Any]) -> bool:
    """Determine if new listing should replace existing one.
    
    Decision based on:
    1. Source priority (lower number = higher priority)
    2. Data completeness (more filled fields = better)
    3. Recency (if same source, newer is better)
    """
    existing_priority = get_source_priority(existing.get('source', ''))
    new_priority = get_source_priority(new.get('source', ''))
    
    # Lower priority number is better
    if new_priority < existing_priority:
        return True
    if new_priority > existing_priority:
        return False
    
    # Same source: compare data completeness
    existing_filled = sum(1 for v in existing.values() if v is not None and v != '')
    new_filled = sum(1 for v in new.values() if v is not None and v != '')
    
    if new_filled > existing_filled:
        return True
    
    # Same completeness: keep existing (first seen wins)
    return False


# Scalar attributes worth borrowing from a duplicate twin when the canonical
# listing lacks them (TIN-520: an olx twin often carries year_built/floor the
# homes.bg canonical doesn't). Higher-priority twins are consulted first.
MERGEABLE_ATTRIBUTES = (
    'floor',
    'total_floors',
    'construction_type',
    'year_built',
    'furnishing',
    'heating',
    'rooms',
    'image_url',
)


def _backfill_missing_attributes(best: Dict[str, Any], duplicates: List[Dict[str, Any]]) -> None:
    """Copy attributes the canonical listing is missing from its twins."""
    donors = sorted(duplicates, key=lambda d: get_source_priority(d.get('source', '')))
    for field in MERGEABLE_ATTRIBUTES:
        if best.get(field) not in (None, '', 0):
            continue
        for donor in donors:
            value = donor.get(field)
            if value not in (None, '', 0):
                best[field] = value
                break


@dataclass
class DeduplicationResult:
    """Result of deduplication process."""
    unique_listings: List[Dict[str, Any]]
    duplicate_listings: List[Dict[str, Any]]
    duplicates_removed: int
    duplicates_by_source: Dict[str, int]
    canonical_ids: Dict[str, str]  # source_id -> canonical_id


def deduplicate_listings(listings: List[Dict[str, Any]]) -> DeduplicationResult:
    """Deduplicate listings across sources.
    
    Groups listings by fingerprint, keeps the "best" source for each,
    and marks others as duplicates.
    
    Args:
        listings: List of listing dictionaries
        
    Returns:
        DeduplicationResult with unique listings and metadata
    """
    # Group by fingerprint
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    
    for listing in listings:
        fingerprint = generate_fingerprint(listing)
        groups[fingerprint].append(listing)
    
    unique_listings = []
    duplicate_listings = []
    duplicates_removed = 0
    duplicates_by_source = defaultdict(int)
    canonical_ids = {}
    
    for fingerprint, group in groups.items():
        canonical_id = generate_canonical_id(fingerprint)
        
        if len(group) == 1:
            # No duplicate
            listing = group[0]
            listing['canonical_id'] = canonical_id
            listing['is_duplicate'] = False
            listing['duplicate_of'] = None
            unique_listings.append(listing)
            canonical_ids[listing.get('source_id', '')] = canonical_id
        else:
            # Find best listing
            best = group[0]
            duplicates = []
            
            for listing in group[1:]:
                if should_replace_existing(best, listing):
                    duplicates.append(best)
                    best = listing
                else:
                    duplicates.append(listing)
            
            # Mark best as unique
            best['canonical_id'] = canonical_id
            best['is_duplicate'] = False
            best['duplicate_of'] = None
            best['duplicate_sources'] = [d.get('source') for d in duplicates]
            _backfill_missing_attributes(best, duplicates)
            unique_listings.append(best)
            canonical_ids[best.get('source_id', '')] = canonical_id
            
            # Mark duplicates
            for dup in duplicates:
                dup['canonical_id'] = canonical_id
                dup['is_duplicate'] = True
                dup['duplicate_of'] = best.get('source_id')
                duplicate_listings.append(dup)
                duplicates_removed += 1
                duplicates_by_source[dup.get('source', 'unknown')] += 1
                canonical_ids[dup.get('source_id', '')] = canonical_id
            
            logger.debug(
                f"Fingerprint {fingerprint}: kept {best.get('source')} "
                f"({len(duplicates)} duplicates from "
                f"{[d.get('source') for d in duplicates]})"
            )
    
    logger.info(
        f"Deduplication: {len(listings)} listings → {len(unique_listings)} unique "
        f"({duplicates_removed} duplicates removed)"
    )
    
    return DeduplicationResult(
        unique_listings=unique_listings,
        duplicate_listings=duplicate_listings,
        duplicates_removed=duplicates_removed,
        duplicates_by_source=dict(duplicates_by_source),
        canonical_ids=canonical_ids,
    )


def get_duplicate_stats(listings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Get statistics about potential duplicates without removing them.
    
    Useful for analyzing overlap between sources.
    """
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    
    for listing in listings:
        fingerprint = generate_fingerprint(listing)
        groups[fingerprint].append(listing)
    
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    
    source_overlap = defaultdict(set)
    for fingerprint, group in duplicates.items():
        sources = tuple(sorted(set(l.get('source', 'unknown') for l in group)))
        source_overlap[sources].add(fingerprint)
    
    return {
        'total_listings': len(listings),
        'unique_fingerprints': len(groups),
        'duplicate_groups': len(duplicates),
        'duplicate_listings': sum(len(v) for v in duplicates.values()),
        'source_combinations': {
            '+'.join(sources): len(fingerprints)
            for sources, fingerprints in source_overlap.items()
        },
    }
