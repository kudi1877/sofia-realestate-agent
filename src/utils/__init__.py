"""Utility functions for Sofia Real Estate Agent."""

from src.utils.deduplication import (
    deduplicate_listings,
    generate_fingerprint,
    generate_canonical_id,
    get_duplicate_stats,
    DeduplicationResult,
)

__all__ = [
    'deduplicate_listings',
    'generate_fingerprint',
    'generate_canonical_id',
    'get_duplicate_stats',
    'DeduplicationResult',
]
