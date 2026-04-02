#!/usr/bin/env python3
"""Quick test for imoti.net scraper fix."""

import sys
sys.path.insert(0, '/Users/tino/clawd/projects/sofia-realestate-agent')

from src.scrapers.imotinet import ImotiNetScraper
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")

print("🧪 Testing imoti.net scraper fix...")
print("=" * 50)

# Test with 10 pages first
scraper = ImotiNetScraper(max_pages=10)
listings = scraper.scrape()

print("=" * 50)
print(f"✅ RESULT: Scraped {len(listings)} listings from 10 pages")
print(f"📊 Expected: 150-300 listings (30 per page)")
print(f"📊 Old result: ~76 listings from 50 pages")

if len(listings) > 100:
    print("🎉 FIX WORKING: Significant improvement!")
else:
    print("⚠️  MAY NEED MORE WORK: Still low yield")

# Show sample
if listings:
    print("\n📍 Sample listings:")
    for i, l in enumerate(listings[:3], 1):
        print(f"  {i}. {l['neighborhood']} | {l['area_sqm']}m² | {l['price_eur']}€")
