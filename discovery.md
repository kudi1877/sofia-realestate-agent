# Discovery — Sofia Real Estate Intelligence Agent
Created: 2026-02-07

## Problem
Tino does real estate investing in Sofia, Bulgaria. Currently relies on manually browsing listings. Wants AI to find underpriced deals, track trends, and alert on opportunities faster than manual scanning.

## Intent
Build an automated system that scrapes Sofia real estate listings, analyzes them for price anomalies (underpriced vs neighborhood average), and alerts Tino to deals worth investigating. Focus on resale/flip opportunities — quick money, not long-term holds.

## Q&A
- **Q:** What neighborhoods? → **A:** All Sofia — don't filter by area
- **Q:** Property types? → **A:** Any — apartments AND plots
- **Q:** Budget range? → **A:** Flexible, can pull capital for good deals. No hard cap.
- **Q:** New build vs resale? → **A:** Resale for apartments (arbitrage opportunity). Plots are always resale.
- **Q:** Investment strategy? → **A:** Resale focus — quick money. Buy underpriced, flip or hold briefly.
- **Q:** How to receive alerts? → **A:** Telegram (primary), dashboard (secondary)

## Scope
**In:**
- Scraping listings from major Bulgarian RE sites (imot.bg, homes.bg, imoti.info, olx.bg)
- Price per sqm analysis by neighborhood
- Underpriced deal detection (statistical anomaly)
- Neighborhood trend tracking (price movement over time)
- Telegram alerts for matching deals
- Dashboard/map visualization

**Out (for now):**
- Rental yield analysis (not resale-focused)
- Mortgage calculators
- Legal/tax advisory
- Properties outside Sofia
- Commercial real estate

## Success Criteria
- [ ] Daily scraping of 4+ RE sites for Sofia listings
- [ ] Price/sqm baseline per neighborhood (updated weekly)
- [ ] Alert within hours when underpriced listing appears (>15% below neighborhood avg)
- [ ] Neighborhood trend charts (weekly price movement)
- [ ] Telegram notifications with listing details + why it's flagged
- [ ] Dashboard with map view and deal tracker

## Dependencies
- Firecrawl API or custom scrapers for Bulgarian RE sites
- Database for storing listings (SQLite or Supabase)
- OpenAI/Kimi for listing analysis
- Telegram bot (already have)
- Map visualization library
