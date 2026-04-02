# Sofia Real Estate Intelligence Agent — Research

Date: 2026-02-07

## 1. Bulgarian Real Estate Site Structure Analysis

### 1.1 Imot.bg (imot.bg)
**Overview:** Bulgaria's largest and oldest RE portal (since 2000). Over 100,000 property listings.

**URL Patterns:**
- Search results: `https://www.imot.bg/index.php?bms=1&folder=1&mode=1&city=1` (Sofia = city 1)
- Apartments: Standard search with `&mode=1` (apartments)
- Plots: `&mode=4` (parcels/plots)
- Price range: `&priceFrom=XX&priceTo=XX`
- Area range: `&areaFrom=XX&areaTo=XX`
- Currency: `&currency=EUR` (or BGN)
- Quarter/Neighborhood: `&quarter=X` (numeric ID)

**Data Available per Listing:**
- Price (BGN/EUR)
- Location (quarter/neighborhood)
- Property type (apartment, house, plot, etc.)
- Square meters (area)
- Construction type (panel, brick, EPK, steel)
- Floor/Floors
- Year built
- Furnishing status (furnished, unfurnished, partially)
- Heating type (central/TEC, local, electric, gas)
- Number of rooms (стай)
- Contact info (agency or private)
- Photos
- Description text

**Anti-Scraping Measures:**
- No public API found
- Traditional HTML scraping needed
- Uses session cookies
- May have rate limiting (likely soft)
- Cloudflare not observed but possible
- Partner site with English translations exists

**Scraping Approach:**
- Requires custom scraper with request rotation
- BeautifulSoup + requests or Playwright for JS rendering
- Session management needed

---

### 1.2 Homes.bg (homes.bg)
**Overview:** Second largest Bulgarian RE portal. Clean UI, structured data.

**URL Patterns:**
- Apartments for sale Sofia: `https://www.homes.bg/obiavi/apartamenti/prodazhbi` (implied Sofia)
- With filters: `?region_id=1&location_id=1`
- Price filters likely in query string

**Data Available per Listing:**
- Price (EUR/BGN shown)
- Price per sqm (EUR/BGN calculated)
- Location (quarter: жк. [neighborhood])
- Property type (едностаен, двустаен, тристаен, четиристаен = studio/1-bed, 2-bed, 3-bed, 4-bed)
- Square meters (e.g., "65m²")
- Construction type (Тухла = brick, Панел = panel, ЕПК = EPK)
- Furnishing (Обзаведен = furnished, Необзаведен = unfurnished, Полуобзаведен = partial)
- Heating (ТЕЦ = central, Локално отопление = local, Електричество = electric)

**Sample Listing Format:**
```
[днес жк. Лозенец, София]
star_outline
Тристаен, 98m²
Тухла, Необзаведен, ТЕЦ
392,000 EUR 4,000EUR/m²
```

**Anti-Scraping Measures:**
- Likely similar to imot.bg
- No public API
- HTML scraping required

**Scraping Approach:**
- Simpler than imot.bg (cleaner HTML structure)
- BeautifulSoup should work well

---

### 1.3 Imoti.info (imoti.info)
**Overview:** Aggregator platform. Claims to combine listings from multiple sources.

**URL Patterns:**
- Main page shows search but URL structure unclear from fetch
- Likely uses query parameters

**Data Available:**
- Similar to other portals
- Interactive map feature mentioned
- Filters by type, price, area

**Anti-Scraping Measures:**
- No public API mentioned
- Likely standard scraping needed

---

### 1.4 OLX.bg (olx.bg/nedvizhimi-imoti)
**Overview:** Classifieds platform with major RE section. Mix of private and agency listings.

**URL Patterns:**
- Real estate section: `https://www.olx.bg/nedvizhimi-imoti/`
- Sofia filter: implied or `?search[city_id]=1`
- Listing URLs: `/d/ad/{slug}-CID368-ID{number}.html`

**Data Available per Listing:**
- Price (BGN typically)
- Location (гр. София, [neighborhood])
- Area (sqm)
- Price per sqm (calculated in listing)
- Property type (apartment, house, plot)
- Date posted/updated
- Seller type (private vs agency sometimes visible)

**Sample Listing Format:**
```
гр. София, Манастирски ливади - Обновено днес в 07:36 ч.
52 кв.м - 769.23 (price per sqm)
```

**Anti-Scraping Measures:**
- Known for anti-scraping (part of larger OLX network)
- Rate limiting likely
- May require proxy rotation
- Could use JavaScript rendering

**Scraping Approach:**
- More challenging than dedicated RE sites
- Playwright/Selenium likely needed
- Consider scraping last (lower priority)

---

### 1.5 Yavlena.com (yavlena.com)
**Overview:** Major real estate agency with own listings. High-end focus.

**URL Patterns:**
- Base: `https://yavlena.com/bg`
- Search uses base64-encoded filters: `?filter=eyJsb2NhdGlvbiI6ImNpdHlfaWQ6NjU4NSIsImNvbnRyYWN0X2lkIjoiMSJ9`

**Data Available:**
- Agency-grade listings
- Likely higher quality data
- Premium properties focus

**Anti-Scraping Measures:**
- Encoded filters suggest some API/backend complexity
- May be easier to scrape than aggregate sites

---

### 1.6 Arco.bg (arco.bg)
**Overview:** Major agency with focus on Sofia market.

**Status:** 
- Could not resolve domain (may use different URL)
- Worth investigating further

---

## 2. Sofia Neighborhood Map (Квартали)

### 2.1 Zones Overview

| Zone | Key Neighborhoods | Price Range (€/sqm) |
|------|-------------------|---------------------|
| **CENTER** | Център, Оборище, Докторски паметник, Иван Вазов | €3,000-5,000+ |
| **SOUTH** | Лозенец, Витоша, Студентски град, Бояна, Драгалевци, Красно село, Манастирски ливади, Кръстова вада | €2,200-4,000 |
| **EAST** | Изток, Гео Милев, Яворов, Подуяне, Слатина, Дружба, Младост (1-4) | €1,800-3,500 |
| **NORTH** | Надежда (1-6), Банишора, Военна рампа, Левски | €1,300-2,000 |
| **WEST** | Люлин (1-10), Овча купел, Горна баня, Красна поляна, Западен парк | €1,400-2,200 |

### 2.2 Complete Neighborhood List

**Center (Централни):**
- Център (Center)
- Оборище (Oborishte)
- Докторски паметник (Doctor's Garden)
- Иван Вазов (Ivan Vazov)

**South (Южни):**
- Лозенец (Lozenets) — VIP, €3,400+/sqm
- Витоша (Vitosha) — mountain adjacency, €2,400-3,200/sqm
- Бояна (Boyana) — luxury villas, €3,500-5,000/sqm
- Драгалевци (Dragalevtsi)
- Красно село (Krasno Selo)
- Манастирски ливади (Manastirski Livadi) — fast growing
- Кръстова вада (Krastova Vada) — metro impact, growing
- Студентски град (Studentski Grad)
- Гоце Делчев (Goce Delchev)
- Борово (Borovo)

**East (Източни):**
- Изток (Iztok) — VIP, €3,500+/sqm
- Гео Милев (Geo Milev)
- Яворов (Yavorov)
- Подуяне (Poduyane)
- Слатина (Slatina)
- Дружба (Druzhba)
- Младост 1-4 (Mladost) — business park, €2,000-2,800/sqm
- Изгрев (Izgrev)
- Дървеница (Darvenitsa)

**North (Северни):**
- Надежда 1, 2, 3, 4 (Nadezhda) — budget, €1,300-1,800/sqm
- Банишора (Banishora)
- Военна рампа (Voenna Rampa)
- Левски (Levski)
- Орландовци (Orlandovtsi)
- Требич (Trebich)

**West (Западни):**
- Люлин 1-10 (Lyulin) — large area, €1,400-2,000/sqm
- Овча купел 1, 2 (Ovcha Kupel)
- Горна баня (Gorna Banya)
- Красна поляна (Krasna Polyana)
- Западен парк (Zapaden Park)
- Суходол (Suhodol)
- Фондови жилища (Fondovi Zhilishta)

### 2.3 Metro Line 3 Expansion Impact

**Current Line 3 Stations:**
- Sofia Airport → city center connection
- Key stations: Sofia Airport, Inter Expo Center, Druzhba, Musagenitsa, G.M. Dimitrov, Orlov Most, National Palace of Culture, Krasno Selo, Ovcha Kupel

**Under Construction (2.8km, 3 stations):**
- Extension to neighborhoods: likely deeper into Ovcha Kupel area
- Expected completion: Check current status

**Price Impact Areas:**
- Areas near new metro stations typically see 5-15% price boost
- Key growth areas: Ovcha Kupel (Line 3), Krastova Vada (Line 3 connections)
- Already seeing impact: Krasno Selo, Manastirski Livadi

---

## 3. Technical Architecture Options

### 3.1 Option A: Firecrawl API (Paid Service)

**Pricing:**
- Free: 500 credits (one-time)
- Hobby: $16/month, 3,000 credits
- Standard: $83/month, 100,000 credits
- Growth: $333/month, 500,000 credits
- Cost per page: 1 credit for scrape/crawl

**Pros:**
- No maintenance of scraping infrastructure
- Handles JavaScript rendering
- Built-in proxy rotation
- Structured data extraction (LLM-based)
- Quick to implement

**Cons:**
- Ongoing monthly cost
- May struggle with Bulgarian language sites
- Rate limits on concurrent requests
- No control over scraping logic

**Estimated Cost for MVP:**
- Daily scrape of 4 sites × 50 listings = 200 pages/day
- Monthly: 6,000 pages
- Hobby plan ($16) insufficient
- Standard plan ($83) provides buffer

### 3.2 Option B: Custom Python Scraper (DIY)

**Stack:**
- Python + BeautifulSoup (static) or Playwright (dynamic)
- SQLite/PostgreSQL for storage
- APScheduler for scheduling
- Proxy rotation (optional: Bright Data, ScrapingBee)

**Pros:**
- Zero recurring cost (except proxies if needed)
- Full control over parsing logic
n- Custom Bulgarian language handling
- Can adapt to site changes quickly

**Cons:**
- Requires maintenance when sites change
- Need to handle rate limiting yourself
- More development time upfront

**Proxy Costs (if needed):**
- Bright Data: ~$3/GB
- ScrapingBee: $49/month for 100,000 API credits
- Free alternative: slow scraping with delays

### 3.3 Option C: ScrapeIt.io (Managed Service)

**Pricing:**
- Custom quote-based for imot.bg specifically
- Estimated: $200-500/month for dedicated scraping

**Pros:**
- Pre-built imot.bg scraper
- Managed infrastructure
- Structured data delivery

**Cons:**
- Most expensive option
- Limited to what they offer
- No control over data format

### 3.4 Recommendation: Hybrid Approach

**MVP Phase:** Custom Python Scraper
- Start with imot.bg and homes.bg (largest, most stable)
- Use BeautifulSoup with request delays
- SQLite for storage (simple, file-based)

**Scale Phase:** Evaluate Firecrawl
- If maintenance becomes burden, migrate to Firecrawl
- Keep custom scraper as fallback

**Rationale:**
- Bulgarian sites need custom parsing anyway (language)
- Starting free allows validation before spending
- Custom scraper can be more precise for RE data

---

## 4. Price Anomaly Detection Methods

### 4.1 Z-Score Based Approach (Recommended for MVP)

**How it works:**
1. Calculate mean price/sqm for each neighborhood + property type + size bucket
2. Calculate standard deviation
3. Flag listings where: `price_per_sqm < (mean - 2×std_dev)`
4. Threshold: 15% below average = potential deal

**Pros:**
- Simple to implement
- Statistical foundation
- Works with limited historical data

**Cons:**
- Assumes normal distribution (not always true)
- Outliers can skew mean

**Implementation:**
```python
def detect_anomaly(listing, neighborhood_stats):
    key = (listing['neighborhood'], listing['type'], listing['size_bucket'])
    mean = neighborhood_stats[key]['mean_price_sqm']
    std = neighborhood_stats[key]['std_price_sqm']
    z_score = (listing['price_sqm'] - mean) / std
    
    if z_score < -1.5:  # 1.5 std dev below mean (~7th percentile)
        return True, (mean - listing['price_sqm']) / mean * 100
    return False, 0
```

### 4.2 Comparable Sales Approach

**How it works:**
1. Find 3-5 similar properties (same neighborhood, type, size ±10%, recent sales)
2. Compare listing price to comparable average
3. Flag if >15% below comparables

**Pros:**
- Most accurate reflection of market
- Used by professional appraisers

**Cons:**
- Requires historical sales data (which we don't have)
- Needs large dataset to find comparables

**Verdict:** Not suitable for MVP (need data first)

### 4.3 Machine Learning Approach

**How it works:**
1. Train regression model on features: neighborhood, type, size, year, floor, etc.
2. Model predicts expected price
3. Flag listings where actual << predicted

**Pros:**
- Can capture complex relationships
- Improves with more data

**Cons:**
- Requires substantial training data
- Overkill for MVP
- Model drift requires retraining

**Verdict:** Phase 3 consideration

### 4.4 Simple Percentile Approach (Alternative)

**How it works:**
1. For each neighborhood + type, calculate price/sqm percentiles
2. Flag listings below 20th percentile
3. Alert on new listings that hit threshold

**Pros:**
- Very simple to implement
- No distribution assumptions
- Fast to compute

**Cons:**
- Less nuanced than Z-score
- May flag too many in cheap neighborhoods

### 4.5 Recommended MVP Approach: Hybrid Z-Score + Percentile

**Algorithm:**
1. Group listings by: `neighborhood + type + size_bucket`
2. Require minimum 10 listings in group for statistical validity
3. Calculate mean, std dev, and 20th percentile
4. Flag if BOTH:
   - Z-score < -1.5 (significantly below mean)
   - Price/sqm < 20th percentile
5. Calculate potential savings %

**Size Buckets:**
- Studio: <45 sqm
- Small 1-bed: 45-55 sqm
- Large 1-bed: 55-70 sqm
- Small 2-bed: 60-80 sqm
- Large 2-bed: 80-100 sqm
- 3-bed: 100-130 sqm
- 4-bed+: >130 sqm

---

## 5. Data Schema (Proposed)

```python
# Listing Model
{
    "id": "imotbg_12345",
    "source": "imot.bg",
    "source_url": "https://...",
    "scraped_at": "2026-02-07T16:00:00Z",
    
    # Location
    "city": "Sofia",
    "neighborhood": "Lozenets",
    "address": "...",
    "latitude": 42.68,
    "longitude": 23.32,
    
    # Property Details
    "property_type": "apartment",  # apartment, house, plot
    "rooms": 3,  # тристайн = 3
    "total_area_sqm": 98,
    "living_area_sqm": 85,
    "floor": 3,
    "total_floors": 6,
    "construction_type": "brick",  # panel, brick, EPK, steel
    "year_built": 2015,
    "furnishing": "unfurnished",
    "heating": "central",
    
    # Pricing
    "price_eur": 392000,
    "price_bgn": None,
    "price_per_sqm_eur": 4000,
    "price_history": [...],
    
    # Listing Metadata
    "listing_type": "sale",  # sale, rent
    "seller_type": "agency",  # agency, private
    "seller_name": "...",
    "published_at": "2026-02-07T10:00:00Z",
    
    # Analysis
    "anomaly_detected": True,
    "anomaly_score": -1.8,
    "neighborhood_avg_price_sqm": 4500,
    "potential_savings_pct": 11,
    "alert_sent": False
}
```

---

## 6. Reference Architecture Analysis

### 6.1 AI Real Estate Agent Team (Shubhamsaboo/awesome-llm-apps)

**Source:** https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/advanced_ai_agents/multi_agent_apps/agent_teams/ai_real_estate_agent_team

**Their Approach:**
- **3-Agent System:**
  1. **Property Search Agent** — Uses Firecrawl Extract API to scrape real estate sites (Zillow, Realtor.com, Trulia, Homes.com)
  2. **Market Analysis Agent** — LLM-powered analysis of market conditions, neighborhood insights, investment outlook
  3. **Property Valuation Agent** — LLM-powered valuation of individual properties (fair price, over/under-priced, investment potential)

- **Data Extraction:**
  - Firecrawl's Extract API with Pydantic schemas for structured data
  - Direct LLM extraction from scraped HTML
  - Schema: address, price, bedrooms, bathrooms, sqft, features, amenities, listing URLs

- **Tech Stack:**
  - **Framework:** Agno (formerly Phidata) for agent orchestration
  - **LLM:** Google Gemini 2.5 Flash (cloud) or gpt-oss:20b via Ollama (local)
  - **Scraping:** Firecrawl API
  - **UI:** Streamlit
  - **Execution:** Sequential (scrape → analyze market → valuate properties)

- **Workflow:**
  1. User inputs location, budget, requirements in Streamlit UI
  2. Property Search Agent scrapes selected sites via Firecrawl
  3. Market Analysis Agent generates market trends and neighborhood insights
  4. Property Valuation Agent evaluates each property for investment potential
  5. Results displayed in Streamlit with progress tracking

**Strengths:**
- Clean multi-agent separation of concerns
- Firecrawl Extract handles messy HTML → structured data conversion
- Pydantic schemas ensure consistent data format
- Works with multiple real estate sites

**Limitations for Our Use Case:**
- Firecrawl may struggle with Bulgarian language sites
- On-demand only (no scheduled/automated scraping)
- No historical tracking or price trends
- No anomaly detection (just general valuation)
- Streamlit UI (we need Telegram + Milo Insights dashboard)
- US-focused sites (Zillow, etc. don't work for Sofia)

### 6.2 Our Adapted Architecture

**Key Differences from Reference:**

| Aspect | Reference Approach | Our Adaptation |
|--------|-------------------|----------------|
| **Target Sites** | Zillow, Realtor.com, Trulia, Homes.com | imot.bg, homes.bg, imoti.info (Bulgarian sites) |
| **Scraping** | Firecrawl Extract API | Hybrid: Custom Python scrapers + optional Firecrawl |
| **LLM** | Gemini 2.5 Flash | Kimi K2.5 or OpenAI (Claude/GPT-4) |
| **Execution** | On-demand (user-triggered) | Scheduled daily cron + on-demand |
| **Storage** | None (real-time only) | SQLite with historical tracking |
| **Alerts** | None | Telegram bot with instant alerts |
| **UI** | Streamlit | Milo Insights dashboard (internal) |
| **Analysis** | General market + valuation | Price anomaly detection + neighborhood trends |

**Our 3-Agent System (Adapted):**

1. **Scraper Agent** (custom, not Firecrawl-dependent)
   - Direct HTTP scraping with httpx + BeautifulSoup
   - Pydantic schemas for Bulgarian site structures
   - SQLite storage with upsert logic
   - Runs on cron schedule (daily 8 AM)

2. **Analysis Agent** (LLM-powered insights)
   - Neighborhood trend analysis
   - Metro impact assessment
   - Market condition summaries
   - **New:** Z-score anomaly detection (statistical, not just LLM)

3. **Alert Agent** (Telegram integration)
   - Filters anomalies by threshold (>15% below average)
   - Formats Telegram messages with deal context
   - Handles quiet hours, batching, deduplication
   - Commands: /stats, /deals, /neighborhood [name]

**Data Flow:**
```
Cron Trigger (8 AM daily)
    ↓
Scraper Agent → imot.bg, homes.bg
    ↓
SQLite Database (new listings + price history)
    ↓
Analysis Agent → Z-score calculation + trend analysis
    ↓
Alert Agent → Filter + Format → Telegram
    ↓
Milo Insights Dashboard (visualization)
```

---

## 7. Key Findings Summary

1. **Best Scraping Targets:** imot.bg and homes.bg (largest volume, most structured)
2. **Most Challenging:** olx.bg (classifieds platform, anti-scraping)
3. **Price Range:** €1,300-5,000/sqm depending on zone
4. **Fastest Growing Areas:** Manastirski Livadi, Krastova Vada (metro impact)
5. **Budget Areas:** Nadezhda, Lyulin, Orlandovtsi (under €1,800/sqm)
6. **VIP Areas:** Lozenets, Iztok, Oborishte, Boyana (€3,500+/sqm)
7. **Recommended Approach:** Custom Python scraper (inspired by reference, but adapted for Bulgarian sites)
8. **Anomaly Detection:** Z-score based with neighborhood + type + size bucketing
9. **Reference Architecture:** Multi-agent pattern from awesome-llm-apps, adapted for scheduled operation + SQLite storage + Telegram alerts
