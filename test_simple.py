#!/usr/bin/env python3
"""Simplified imoti.net test with progress output."""

import sys
sys.path.insert(0, '/Users/tino/clawd/projects/sofia-realestate-agent')

from src.scrapers.imotinet import ImotiNetScraper
import time

print("🧪 Testing imoti.net scraper (5 pages max)")
print("-" * 40, flush=True)

scraper = ImotiNetScraper(max_pages=5)
start = time.time()

try:
    listings = scraper.scrape()
    elapsed = time.time() - start
    
    print("-" * 40, flush=True)
    print(f"✅ SUCCESS: {len(listings)} listings in {elapsed:.1f}s", flush=True)
    print(f"📊 Per page: {len(listings)/5:.1f} avg", flush=True)
    print(f"📊 Projected (366 pages): ~{int(len(listings) * 73.2):,} listings", flush=True)
    
    if listings:
        print(f"\n📍 First 3 listings:", flush=True)
        for l in listings[:3]:
            print(f"  • {l['neighborhood']} | {l['area_sqm']}m² | {l['price_eur']:,.0f}€", flush=True)
            
except Exception as e:
    print(f"❌ ERROR: {e}", flush=True)
    import traceback
    traceback.print_exc()
