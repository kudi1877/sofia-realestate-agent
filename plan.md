# Sofia Real Estate Intelligence Agent — Implementation Plan

Date: 2026-02-07
**Reference:** Adapted from [AI Real Estate Agent Team](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/advanced_ai_agents/multi_agent_apps/agent_teams/ai_real_estate_agent_team) by Shubhamsaboo

---

## Architecture Overview

Our architecture is inspired by the **3-Agent pattern** from the reference repo, but heavily adapted for:
- **Bulgarian real estate sites** (imot.bg, homes.bg) instead of Zillow/Realtor.com
- **Scheduled automated scraping** (daily cron) instead of on-demand only
- **SQLite persistence** for historical tracking and trend analysis
- **Telegram alerts** for real-time deal notifications
- **Milo Insights dashboard** instead of Streamlit
- **Statistical anomaly detection** (Z-score) + **LLM-powered insights** (Kimi/OpenAI)

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           TRIGGER LAYER                                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌─────────────────────┐         ┌─────────────────────┐                       │
│   │   Cron Scheduler    │         │   On-Demand API     │                       │
│   │   (Daily 08:00)     │         │   (Manual Trigger)  │                       │
│   └──────────┬──────────┘         └──────────┬──────────┘                       │
│              │                               │                                   │
└──────────────┼───────────────────────────────┼───────────────────────────────────┘
               │                               │
               └───────────────┬───────────────┘
                               │
┌──────────────────────────────┼───────────────────────────────────────────────────┐
│                              ▼                                                   │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │                    AGENT TEAM (3-Agent System)                           │   │
│   ├─────────────────────────────────────────────────────────────────────────┤   │
│   │                                                                         │   │
│   │  ┌─────────────────────────────────────────────────────────────────┐   │   │
│   │  │                    AGENT 1: SCRAPER AGENT                        │   │   │
│   │  ├─────────────────────────────────────────────────────────────────┤   │   │
│   │  │  • Target: imot.bg, homes.bg, imoti.info                        │   │   │
│   │  │  • Method: httpx + BeautifulSoup (custom scrapers)              │   │   │
│   │  │  • Schema: Pydantic models for Bulgarian RE data                │   │   │
│   │  │  • Output: Structured Listing objects                           │   │   │
│   │  │  • Storage: SQLite with upsert logic                            │   │   │
│   │  │  • Schedule: Daily cron at 08:00                                │   │   │
│   │  └─────────────────────────────────────────────────────────────────┘   │   │
│   │                                    │                                    │   │
│   │                                    ▼                                    │   │
│   │  ┌─────────────────────────────────────────────────────────────────┐   │   │
│   │  │                    AGENT 2: ANALYSIS AGENT                       │   │   │
│   │  ├─────────────────────────────────────────────────────────────────┤   │   │
│   │  │  • Statistical Analysis:                                        │   │   │
│   │  │    - Z-score calculation per neighborhood+type+size             │   │   │
│   │  │    - Anomaly detection (>15% below average)                     │   │   │
│   │  │    - Price trend tracking (weekly deltas)                       │   │   │
│   │  │  • LLM Analysis (Kimi K2.5 / OpenAI):                           │   │   │
│   │  │    - Market condition summary                                   │   │   │
│   │  │    - Neighborhood insights (metro impact, etc.)                 │   │   │
│   │  │    - Investment potential assessment                            │   │   │
│   │  │  • Output: Analyzed listings with anomaly scores                │   │   │
│   │  └─────────────────────────────────────────────────────────────────┘   │   │
│   │                                    │                                    │   │
│   │                                    ▼                                    │   │
│   │  ┌─────────────────────────────────────────────────────────────────┐   │   │
│   │  │                    AGENT 3: ALERT AGENT                          │   │   │
│   │  ├─────────────────────────────────────────────────────────────────┤   │   │
│   │  │  • Filter: Threshold-based filtering (>15% savings)             │   │   │
│   │  │  • Deduplication: Prevent duplicate alerts                      │   │   │
│   │  │  • Rate Limiting: Quiet hours (22:00-08:00), batching           │   │   │
│   │  │  • Format: Telegram-friendly message templates                  │   │   │
│   │  │  • Delivery: Telegram Bot API                                   │   │   │
│   │  │  • Commands: /start, /stats, /deals, /neighborhood [name]       │   │   │
│   │  └─────────────────────────────────────────────────────────────────┘   │   │
│   │                                                                         │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                              DATA LAYER                                           │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌─────────────────────────────┐  ┌─────────────────────────────┐              │
│   │      SQLite Database        │  │      Price History Log      │              │
│   ├─────────────────────────────┤  ├─────────────────────────────┤              │
│   │  • listings (current)       │  │  • Historical prices        │              │
│   │  • neighborhoods (stats)    │  │  • Trend data               │              │
│   │  • alerts (sent tracking)   │  │  • Market snapshots         │              │
│   │  • metadata (scraping runs) │  │                             │              │
│   └─────────────────────────────┘  └─────────────────────────────┘              │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             OUTPUT LAYER                                          │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌─────────────────────────────┐  ┌─────────────────────────────┐              │
│   │    Telegram Bot             │  │    Milo Insights Dashboard  │              │
│   ├─────────────────────────────┤  ├─────────────────────────────┤              │
│   │  • Instant deal alerts      │  │  • Map view (price heatmap) │              │
│   │  • Daily digest (9 AM)      │  │  • Neighborhood trends      │              │
│   │  • Query commands           │  │  • Deal tracker             │              │
│   │  • /stats, /deals, /trends  │  │  • Market reports           │              │
│   └─────────────────────────────┘  └─────────────────────────────┘              │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## Tech Stack Recommendation

### Core Stack (Adapted from Reference)
| Component | Reference | Our Adaptation | Rationale |
|-----------|-----------|----------------|-----------|
| **Framework** | Agno | Custom orchestration | Simpler, no extra dependency |
| **LLM** | Gemini 2.5 Flash | Kimi K2.5 / OpenAI | Better Bulgarian language support |
| **Scraping** | Firecrawl Extract | httpx + BeautifulSoup | Bulgarian sites need custom parsing |
| **Data Schema** | Pydantic | Pydantic | Keep from reference - excellent for structured data |
| **Storage** | None (real-time) | SQLite + Price History | Need historical tracking for trends |
| **Scheduling** | On-demand | APScheduler (cron) | Automated daily scraping |
| **Alerts** | None | Telegram Bot | Real-time deal notifications |
| **Dashboard** | Streamlit | Milo Insights | Internal dashboard integration |

### Python Dependencies
```
# Core
httpx>=0.27.0
beautifulsoup4>=4.12.0
pydantic>=2.5.0
python-dotenv>=1.0.0
loguru>=0.7.0

# Database
sqlalchemy>=2.0.0
alembic>=1.13.0

# Scheduling
apscheduler>=3.10.0

# Telegram
python-telegram-bot>=20.0

# Analysis
pandas>=2.1.0
numpy>=1.26.0
scipy>=1.11.0

# LLM (choose one or both)
openai>=1.10.0
# kimi/kimi-python (when available)

# Dashboard (Milo Insights integration)
# (Uses existing Milo stack)
```

### Project Structure
```
sofia-realestate-agent/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
├── alembic/
│   └── versions/
├── data/
│   └── listings.db                 # SQLite database
├── src/
│   ├── __init__.py
│   ├── config.py                   # Settings & env vars
│   ├── models.py                   # Pydantic schemas (from reference pattern)
│   ├── database/
│   │   ├── __init__.py
│   │   ├── db.py                   # SQLAlchemy setup
│   │   ├── models.py               # SQLAlchemy models
│   │   └── repository.py           # Data access layer
│   ├── agents/                     # 3-Agent System
│   │   ├── __init__.py
│   │   ├── base.py                 # Base agent class
│   │   ├── scraper_agent.py        # Agent 1: Scraper
│   │   ├── analysis_agent.py       # Agent 2: Analysis
│   │   └── alert_agent.py          # Agent 3: Alert
│   ├── scrapers/
│   │   ├── __init__.py
│   │   ├── base.py                 # Base scraper
│   │   ├── imotbg.py               # Imot.bg scraper
│   │   ├── homesbg.py              # Homes.bg scraper
│   │   └── runner.py               # Scraper orchestrator
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── calculator.py           # Price/sqm calculations
│   │   ├── anomaly.py              # Z-score anomaly detection
│   │   ├── trends.py               # Trend analysis
│   │   └── llm_insights.py         # LLM-powered market insights
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── telegram.py             # Telegram bot handler
│   │   ├── formatter.py            # Message formatting
│   │   └── filter.py               # Alert filtering logic
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py               # LLM client wrapper
│   │   └── prompts.py              # Prompt templates
│   └── main.py                     # Entry point
└── scripts/
    ├── run_scraper.sh              # Manual trigger
    └── migrate.sh                  # DB migrations
```

---

## Pydantic Schema (Inspired by Reference)

```python
# src/models.py

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum

class PropertyType(str, Enum):
    APARTMENT = "apartment"
    HOUSE = "house"
    PLOT = "plot"
    STUDIO = "studio"

class ConstructionType(str, Enum):
    PANEL = "panel"      # Панел
    BRICK = "brick"      # Тухла
    EPK = "epk"          # ЕПК
    STEEL = "steel"      # Сглобяема

class Listing(BaseModel):
    """Pydantic schema for real estate listings (adapted from reference)"""
    
    # IDs
    id: str = Field(..., description="Unique ID: source_listingId")
    source: str = Field(..., description="Source: imot.bg, homes.bg, etc.")
    source_url: str = Field(..., description="Original listing URL")
    
    # Location
    city: str = "Sofia"
    neighborhood: str = Field(..., description="Квартал: Лозенец, Младост, etc.")
    address: Optional[str] = None
    
    # Property Details
    property_type: PropertyType
    rooms: int = Field(..., description="Number of rooms (стай)")
    total_area_sqm: float
    living_area_sqm: Optional[float] = None
    floor: Optional[int] = None
    total_floors: Optional[int] = None
    construction_type: Optional[ConstructionType] = None
    year_built: Optional[int] = None
    furnishing: Optional[str] = None  # Обзаведен, Необзаведен
    heating: Optional[str] = None     # ТЕЦ, Локално, etc.
    
    # Pricing
    price_eur: float
    price_per_sqm_eur: float
    
    # Metadata
    seller_type: str  # agency, private
    seller_name: Optional[str] = None
    published_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Analysis (populated by Analysis Agent)
    anomaly_score: Optional[float] = None  # Z-score
    neighborhood_avg_price_sqm: Optional[float] = None
    potential_savings_pct: Optional[float] = None
    is_anomaly: bool = False
    
    class Config:
        from_attributes = True

class MarketAnalysis(BaseModel):
    """LLM-generated market analysis"""
    market_condition: str = Field(..., description="Buyer's/Seller's/Balanced market")
    price_trend: str = Field(..., description="Rising/Stable/Falling")
    key_insights: List[str] = Field(..., max_length=3)
    investment_outlook: str = Field(..., max_length=200)
    metro_impact: Optional[str] = None

class PropertyValuation(BaseModel):
    """LLM-generated property valuation"""
    valuation: str = Field(..., description="Fair price / Overpriced / Underpriced")
    investment_potential: str = Field(..., description="High / Medium / Low")
    key_factors: List[str] = Field(..., max_length=3)
    recommendation: str = Field(..., max_length=100)
```

---

## Implementation Phases

### Phase 1: MVP — Core Agent Team (Week 1-2)

**Goal:** Functional 3-Agent system with Telegram alerts

**Reference Adaptation:** Build the 3-Agent structure but with custom scrapers instead of Firecrawl

**Tasks:**

**Day 1: Foundation**
- [ ] Set up project structure (based on reference repo layout)
- [ ] Create Pydantic schemas (Listing, MarketAnalysis, PropertyValuation)
- [ ] SQLAlchemy models + Alembic migrations
- [ ] SQLite database setup

**Day 2: Agent 1 — Scraper Agent**
- [ ] Build `ScraperAgent` class (base structure from reference pattern)
- [ ] Imot.bg scraper (httpx + BeautifulSoup)
- [ ] Parse listing pages with Pydantic validation
- [ ] SQLite upsert logic (avoid duplicates)
- [ ] Rate limiting (1 req/sec)

**Day 3: Agent 1 — More Scrapers**
- [ ] Homes.bg scraper
- [ ] Scraper runner with progress logging
- [ ] Error handling & retries
- [ ] Cron scheduler setup (APScheduler)

**Day 4: Agent 2 — Analysis Agent (Statistical)**
- [ ] Calculate price/sqm for all listings
- [ ] Group by neighborhood + type + size bucket
- [ ] Z-score calculation
- [ ] Anomaly detection (>15% below mean)
- [ ] Store anomaly scores in DB

**Day 5: Agent 2 — Analysis Agent (LLM)**
- [ ] LLM client wrapper (Kimi/OpenAI)
- [ ] Market analysis prompt engineering
- [ ] Neighborhood insights generation
- [ ] Investment potential assessment
- [ ] Combine statistical + LLM analysis

**Day 6: Agent 3 — Alert Agent**
- [ ] Telegram bot setup
- [ ] Alert filtering (threshold >15%)
- [ ] Message formatting (deal cards)
- [ ] Deduplication logic
- [ ] Quiet hours implementation

**Day 7: Integration & Testing**
- [ ] Agent orchestration (Scraper → Analysis → Alert)
- [ ] End-to-end testing
- [ ] First Telegram alerts
- [ ] Bug fixes & tuning

**Deliverables:**
- ✅ 3-Agent system running
- ✅ Daily automated scraping at 8 AM
- ✅ Telegram alerts for underpriced deals
- ✅ SQLite database with listings

**Estimated Effort:** 7 days

---

### Phase 2: Intelligence + Dashboard (Week 3-4)

**Goal:** Price trends, Milo Insights dashboard, additional scrapers

**Tasks:**

**Day 1-3: Price History & Trends**
- [ ] Price history tracking (same listing, different prices)
- [ ] Weekly trend calculations (neighborhood price movement)
- [ ] Metro impact zone detection
- [ ] Price drop alerts (listing reduced price)

**Day 4-5: Additional Scrapers**
- [ ] Imoti.info scraper
- [ ] Deduplication across sources
- [ ] Data validation rules

**Day 6-8: Milo Insights Dashboard**
- [ ] Map view (price heatmap by neighborhood)
- [ ] Neighborhood comparison charts
- [ ] Deal tracker (historical anomalies)
- [ ] Trend visualization
- [ ] Filter by neighborhood, type, price

**Day 9-10: Polish**
- [ ] False positive tuning
- [ ] Alert threshold adjustments
- [ ] Documentation

**Deliverables:**
- ✅ Price trend tracking
- ✅ Milo Insights dashboard
- ✅ 3 data sources
- ✅ Price drop alerts

**Estimated Effort:** 10 days

---

### Phase 3: Advanced Intelligence (Week 5-6)

**Goal:** ML prediction, market reports, API

**Tasks:**

**Day 1-4: ML Price Prediction**
- [ ] Feature engineering (neighborhood, type, size, year, floor, metro)
- [ ] Train regression model on historical data
- [ ] Predict expected price for new listings
- [ ] Detect listings significantly below predicted price

**Day 5-7: Market Intelligence**
- [ ] Weekly market reports (auto-generated)
- [ ] Hot neighborhoods identification
- [ ] Supply/demand indicators
- [ ] Days-on-market tracking

**Day 8-10: API & Advanced Features**
- [ ] REST API for listing queries
- [ ] Webhook support
- [ ] Custom alert rules
- [ ] Saved searches

**Deliverables:**
- ✅ ML price prediction
- ✅ Weekly market reports
- ✅ REST API
- ✅ Advanced alert rules

**Estimated Effort:** 10 days

---

## Agent Specifications

### Agent 1: Scraper Agent

**Role:** Collect property listings from Bulgarian RE sites

**Input:** Cron trigger or manual API call
**Output:** List of `Listing` objects stored in SQLite

**Implementation:**
```python
class ScraperAgent:
    """Agent 1: Scrapes Bulgarian RE sites"""
    
    def __init__(self):
        self.scrapers = [ImotBgScraper(), HomesBgScraper()]
    
    async def run(self) -> List[Listing]:
        """Run all scrapers and return listings"""
        all_listings = []
        for scraper in self.scrapers:
            listings = await scraper.scrape()
            all_listings.extend(listings)
        
        # Upsert to database
        await self.store_listings(all_listings)
        return all_listings
```

**Key Features:**
- Custom scrapers for Bulgarian sites (not Firecrawl)
- Pydantic validation for data consistency
- SQLite upsert (avoid duplicates)
- Rate limiting (1 req/sec)
- Error recovery (continue on failure)

---

### Agent 2: Analysis Agent

**Role:** Analyze listings for anomalies and market insights

**Input:** New listings from database
**Output:** Analyzed listings with anomaly scores + market insights

**Implementation:**
```python
class AnalysisAgent:
    """Agent 2: Statistical + LLM analysis"""
    
    def __init__(self, llm_client):
        self.llm = llm_client
    
    async def run(self, listings: List[Listing]) -> List[Listing]:
        """Analyze listings and detect anomalies"""
        
        # Statistical Analysis
        stats = self.calculate_neighborhood_stats()
        for listing in listings:
            listing.anomaly_score = self.calculate_z_score(listing, stats)
            listing.is_anomaly = listing.anomaly_score < -1.5
        
        # LLM Analysis for anomalies only
        anomalies = [l for l in listings if l.is_anomaly]
        for listing in anomalies:
            insights = await self.llm.analyze_property(listing)
            listing.llm_insights = insights
        
        return listings
```

**Key Features:**
- Z-score calculation per neighborhood+type+size
- Threshold: Z-score < -1.5 = anomaly
- LLM insights for anomalies only (save tokens)
- Market condition summaries
- Metro impact analysis

---

### Agent 3: Alert Agent

**Role:** Send Telegram alerts for deals

**Input:** Analyzed listings with anomaly flags
**Output:** Telegram messages sent

**Implementation:**
```python
class AlertAgent:
    """Agent 3: Telegram alerts"""
    
    def __init__(self, bot_token):
        self.bot = telegram.Bot(token)
        self.chat_id = config.TELEGRAM_CHAT_ID
    
    async def run(self, listings: List[Listing]):
        """Send alerts for anomalies"""
        
        anomalies = [l for l in listings if l.is_anomaly]
        
        for listing in anomalies:
            # Check if alert already sent
            if await self.alert_exists(listing.id):
                continue
            
            # Check quiet hours
            if self.is_quiet_hours():
                await self.queue_alert(listing)
                continue
            
            # Send alert
            message = self.format_alert(listing)
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='Markdown'
            )
            await self.mark_alert_sent(listing.id)
```

**Key Features:**
- Threshold filtering (>15% savings)
- Deduplication (don't alert twice)
- Quiet hours (22:00-08:00)
- Batching (max 5 alerts at once)
- Rich formatting with emojis

---

## What Can Be Built Immediately vs What Needs More Data

### Immediate (Week 1)

| Feature | Status | Notes |
|---------|--------|-------|
| 3-Agent structure | ✅ Ready | Based on reference pattern |
| Pydantic schemas | ✅ Ready | Adapted from reference |
| Imot.bg scraper | ✅ Ready | Custom for Bulgarian site |
| SQLite storage | ✅ Ready | Simple schema |
| Z-score anomaly | ✅ Ready | Works with ~50 listings |
| Telegram alerts | ✅ Ready | Bot framework ready |
| Basic LLM insights | ✅ Ready | Kimi/OpenAI available |

### Needs Initial Data (Week 2)

| Feature | Minimum Data Needed | Notes |
|---------|---------------------|-------|
| Neighborhood baselines | 50+ listings/area | For Z-score calculation |
| Price trends | 2+ weeks history | Week-over-week deltas |
| Duplicate detection | 2+ sources | Cross-reference IDs |
| Price drop alerts | Historical tracking | Same listing, new price |
| LLM market summaries | 200+ listings | Context for insights |

### Needs Substantial Data (Week 4+)

| Feature | Minimum Data Needed | Notes |
|---------|---------------------|-------|
| ML price prediction | 1000+ listings | Training dataset |
| Hot neighborhoods | 4+ weeks data | Reliable trends |
| Days-on-market | Time-series | First seen vs sold |
| Market reports | 1+ month data | Meaningful summaries |
| Metro impact analysis | Before/after metro | Causal inference |

---

## Telegram Commands

```
/start - Welcome message + help
/stats - Market statistics summary
/deals - Recent anomaly deals
/neighborhood [name] - Stats for specific area
/trends - Price trend charts
/track [id] - Monitor specific listing
/stop - Pause alerts
```

**Alert Format:**
```
🏠 DEAL ALERT: Underpriced Apartment Detected!

📍 Location: жк. Лозенец, София
🏢 Type: 3-room (тристайн), 98m²
🏗️ Construction: Brick (Тухла), 2015
💶 Price: €392,000 (€4,000/m²)

📊 Market Analysis:
   • Avg in Lozenets (3-bed): €4,500/m²
   • Potential savings: €49,000 (11%)
   • Z-score: -1.8 (significantly below average)

🤖 AI Insights:
   Market condition: Seller's market with rising prices.
   Investment potential: High - Lozenets is premium area
   Key factor: Metro Line 3 nearby adds 5-10% value

🔗 View Listing: https://imot.bg/...
⚡ Posted: Today at 10:30 AM

Reply /track to monitor price changes
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Site blocks scraper | Medium | High | Rate limiting, rotate IPs |
| Bulgarian language parsing | Medium | High | Custom parsers, test thoroughly |
| LLM API costs | Medium | Medium | Only analyze anomalies, not all listings |
| False positive alerts | Medium | Medium | Tuning thresholds, user feedback |
| SQLite performance | Low | Medium | Migrate to Postgres at scale |

---

## Success Metrics

| Metric | Phase 1 Target | Phase 2 Target | Phase 3 Target |
|--------|----------------|----------------|----------------|
| Listings/day scraped | 100 | 300 | 500 |
| Alert accuracy | 70% | 80% | 85% |
| Avg response time (alert) | <5 min | <2 min | <1 min |
| Data freshness | Daily | 12 hours | 6 hours |
| Dashboard uptime | N/A | 99% | 99.5% |
| LLM cost per day | <$1 | <$2 | <$3 |

---

## Next Steps

1. **Today:**
   - Clone reference repo for code patterns
   - Set up project structure
   - Create Pydantic schemas

2. **This Week:**
   - Build Scraper Agent (imot.bg)
   - Implement Analysis Agent (Z-score)
   - Create Alert Agent (Telegram)
   - First end-to-end test

3. **Review Point (End of Phase 1):**
   - Assess false positive rate
   - Tune Z-score thresholds
   - Evaluate if Firecrawl would help
   - Plan Phase 2 priorities

---

## Reference Documentation

**Original Inspiration:**
- Repo: https://github.com/Shubhamsaboo/awesome-llm-apps
- Path: `advanced_ai_agents/multi_agent_apps/agent_teams/ai_real_estate_agent_team`
- Key learnings:
  - 3-Agent pattern works well for RE analysis
  - Pydantic schemas essential for structured data
  - Sequential execution is simpler than parallel
  - LLM insights add significant value

**Our Adaptations:**
- Custom scrapers instead of Firecrawl (Bulgarian sites)
- SQLite storage instead of real-time only
- Cron scheduling instead of on-demand
- Telegram alerts instead of just Streamlit
- Statistical anomaly detection + LLM insights
