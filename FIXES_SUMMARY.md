# Sofia Real Estate Agent - Critical Fixes Summary

## Changes Implemented

### PRIORITY 1: Fixed Scraper Yields

#### 1. homesbg.py - Fixed API Pagination
**Problem:** API was returning the same 20 listings regardless of page number.

**Root Cause:** The homes.bg API uses `from` parameter for offset-based pagination instead of `page`.

**Fix:**
- Changed from `?page=N` to `?from={offset}` where offset = (page-1) * 20
- Added proper pagination loop with consecutive empty page detection
- Increased max_pages from 10 to 50 (potential for 1000 listings)
- Added better headers including Referer

**Key Changes:**
```python
# OLD: url = f"{self.API_URL}?page={page}"
# NEW: 
offset = (page - 1) * self.RESULTS_PER_PAGE
url = f"{self.API_URL}?from={offset}"
```

#### 2. imotinet.py - Switched to Bulgarian Version
**Problem:** Only getting 76 listings from 50 pages using English version.

**Root Cause:** The English version (`/en/obiavi/`) has limited coverage compared to Bulgarian version.

**Fix:**
- Changed SOFIA_URL from `/en/obiavi/r/prodava/sofia/` to `/bg/obiavi/r/prodava/sofia/`
- Updated selectors to handle both English and Bulgarian text patterns
- Added more robust container selectors for listing cards
- Improved price parsing for Bulgarian formats with "лв" suffix

#### 3. propertybg.py - Fixed Encoding and Selectors
**Problem:** Only getting 31 listings.

**Root Cause:** Site has been modernized - now uses UTF-8 encoding instead of windows-1251.

**Fix:**
- Changed encoding from `windows-1251` to `utf-8`
- Added fallback to auto-detection if UTF-8 fails
- Updated selectors to match current site structure
- Added more container selectors for robustness
- Improved neighborhood extraction for Bulgarian names

---

### PRIORITY 2: Cross-Source Deduplication

#### New Module: src/utils/deduplication.py

**Features:**
- **Fingerprint Generation:** Creates unique identifiers based on:
  - Normalized neighborhood name
  - Area rounded to nearest 5 sqm
  - Number of rooms
  - Price range (5% bands)

- **Source Priority Ranking:**
  1. imoti.info (highest quality)
  2. imot.bg (good coverage)
  3. homesbg (medium quality)
  4. imotinet (lower coverage)
  5. propertybg (lowest priority)

- **Deduplication Logic:**
  - Groups listings by fingerprint
  - Keeps highest priority source
  - Marks duplicates with canonical_id

#### Database Model Updates: src/database/models.py

**New Fields on Listing Table:**
- `canonical_id` (String, indexed) - Fingerprint-based unique ID
- `is_duplicate` (Boolean) - Whether this is a duplicate
- `duplicate_of` (String) - source_id of primary listing

**New Field on Neighborhood Table:**
- `unique_listing_count` - Count after deduplication

#### Integration: src/main.py

- Scrapers now collect all listings first
- Deduplication runs before database save
- Stats calculation uses only unique listings
- Added `dedup-stats` command to view overlap stats

---

### PRIORITY 3: Telegram Alerts Integration

#### New Module: src/alerts/telegram.py

**Features:**
- **Deal Alert Formatting:**
  ```
  🏠 DEAL: {neighborhood} | {price}€ | {savings}% below avg
  ```

- **Detailed Alert Format:**
  - Location
  - Price with per-sqm calculation
  - Size and room count
  - Savings percentage and amount
  - Z-score with deal quality indicator
  - Source attribution
  - Direct link to listing

- **Alert Thresholds:**
  - Default: zscore < -1.5 (1.5 std dev below mean)
  - Visual indicators: 🔥 (>25%), 💰 (>15%), 🏠 (standard)

#### New Module: src/message_sender.py

**Integration with OpenClaw:**
- Handles message preparation for Telegram
- Target: Tino's Telegram (ID: 1787160163)
- Supports deal alerts and daily digest

#### Integration: src/main.py

- `cmd_alerts()` now sends via message tool
- Only sends alerts with zscore < -1.5
- Marks alerts as sent in database after sending

---

## Files Modified/Created

### Modified Files:
1. `src/scrapers/homesbg.py` - Fixed pagination
2. `src/scrapers/imotinet.py` - Switched to Bulgarian version
3. `src/scrapers/propertybg.py` - Fixed encoding and selectors
4. `src/database/models.py` - Added canonical_id, is_duplicate fields
5. `src/main.py` - Integrated dedup and alerts

### New Files:
1. `src/utils/__init__.py` - Utils package init
2. `src/utils/deduplication.py` - Deduplication logic
3. `src/alerts/telegram.py` - Telegram alert formatting
4. `src/alerts/__init__.py` - Alerts package init
5. `src/message_sender.py` - OpenClaw message integration

---

## Expected Results

### Before Fixes:
- homes.bg: ~40 listings
- imoti.net: ~76 listings  
- property.bg: ~31 listings
- Total: ~147 listings from these 3 sources

### After Fixes:
- homes.bg: Up to 1000 listings (50 pages × 20)
- imoti.net: Expected 1000+ listings (Bulgarian version has better coverage)
- property.bg: Expected 500+ listings (proper encoding)
- **Deduplication:** Will identify and merge ~20-30% duplicates across sources

### Alert Flow:
1. Scraper collects listings from all sources
2. Deduplication creates unique set with canonical IDs
3. Analysis detects underpriced listings (zscore < -1.5)
4. Alerts generated and sent to Telegram
5. Database tracks which alerts have been sent

---

## Usage

### Run Scrapers with Deduplication:
```bash
python -m src.main scrape
```

### View Deduplication Stats:
```bash
python -m src.main dedup-stats
```

### Run Full Pipeline:
```bash
python -m src.main full
```

### Send Pending Alerts:
```bash
python -m src.main alerts
```

---

## Testing Recommendations

1. **Test scrapers individually:**
   ```bash
   python -c "from src.scrapers.homesbg import HomesBgScraper; s = HomesBgScraper(max_pages=3); print(len(s.scrape()))"
   ```

2. **Test deduplication:**
   ```bash
   python -m src.main dedup-stats
   ```

3. **Test alerts (dry run):**
   ```bash
   python -m src.main analyze
   python -m src.main alerts
   ```
