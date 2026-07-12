"""Main entry point for Sofia Real Estate Agent."""

import sys
import argparse
from typing import Any, Dict, List

from loguru import logger

from src.database.models import init_db, get_db
from src.database.repository import (
    AlertRepository,
    ListingRepository,
    NeighborhoodRepository,
    NeighborhoodStatsHistoryRepository,
)
from src.scrapers.imotbg import ImotBgScraper
from src.scrapers.homesbg import HomesBgScraper
from src.scrapers.imotiinfo import ImotiInfoScraper
from src.scrapers.imotinet import ImotiNetScraper
from src.scrapers.propertybg import PropertyBGScraper
from src.analysis.anomaly import analyze_database, calculate_neighborhood_stats
from src.analysis.trends import calculate_neighborhood_trends, generate_market_summary
from src.alerts.telegram import should_send_alert
from src.config import MARK_INACTIVE_MIN_RATIO, PUBLISH_MIN_MEDIAN_EUR_SQM, SOLD_AFTER_DAYS
from src.utils.deduplication import deduplicate_listings, get_duplicate_stats


# Configure logging
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


def cmd_scrape(recorder=None):
    """Run all scrapers with deduplication.

    Optional `recorder` is a RunRecorder from src.observability — when present,
    per-source results are appended to it for runs.json. Uses introspectable
    SCRAPERS list so we don't repeat the try/except pattern five times.
    """
    import time as _time
    from src.observability import SourceResult

    logger.info("Starting scraping...")

    db = get_db()
    repo = ListingRepository(db)

    all_listings = []
    active_source_ids = {}

    # (display_name, source_key, scraper_factory)
    # Each factory returns either a context-manager scraper or a plain instance.
    SCRAPERS = [
        ("imot.bg",     "imotbg",     lambda: ImotBgScraper()),
        ("homes.bg",    "homesbg",    lambda: HomesBgScraper()),
        ("imoti.info",  "imotiinfo",  lambda: ImotiInfoScraper()),
        ("imoti.net",   "imotinet",   lambda: ImotiNetScraper()),
        ("property.bg", "propertybg", lambda: PropertyBGScraper()),
    ]

    for display_name, source_key, factory in SCRAPERS:
        t0 = _time.time()
        try:
            scraper = factory()
            # Use context manager when supported (httpx-based scrapers)
            if hasattr(scraper, "__enter__"):
                with scraper as s:
                    listings = s.scrape()
            else:
                listings = scraper.scrape()
            logger.info(f"{display_name}: scraped {len(listings)} listings")
            all_listings.extend(listings)
            active_source_ids[source_key] = [l['source_id'] for l in listings]
            if recorder is not None:
                status = "ok" if listings else "empty"
                recorder.add_source(SourceResult(
                    name=display_name, scraped=len(listings),
                    duration_sec=round(_time.time() - t0, 1), status=status,
                ))
        except Exception as e:
            logger.error(f"Error scraping {display_name}: {e}")
            if recorder is not None:
                recorder.add_source(SourceResult(
                    name=display_name, scraped=0,
                    duration_sec=round(_time.time() - t0, 1),
                    status="error", error=str(e)[:200],
                ))
                recorder.add_error(f"{display_name}: {str(e)[:200]}")
    
    # Canonicalize neighborhood names before dedup/stats (TIN-468): merges
    # imoti.net's Latin slugs ("Bankja") and prefixed variants ("гр. Банкя")
    # into one canonical Cyrillic group per place.
    from src.utils.neighborhoods import canonicalize_neighborhood
    from src.config import MIN_PRICE_EUR
    for listing in all_listings:
        listing['neighborhood'] = canonicalize_neighborhood(listing.get('neighborhood'))

    # Price sanity floor (TIN-472): sub-floor prices are parse artifacts
    # (a €6 "listing" once made Top Pick of the Day).
    before = len(all_listings)
    all_listings = [l for l in all_listings if (l.get('price_eur') or 0) >= MIN_PRICE_EUR]
    if before - len(all_listings):
        logger.warning(f"Dropped {before - len(all_listings)} listings below €{MIN_PRICE_EUR:,.0f} price floor")

    # Deduplicate listings before saving
    logger.info(f"Total raw listings: {len(all_listings)}")
    dedup_result = deduplicate_listings(all_listings)
    logger.info(
        f"After deduplication: {len(dedup_result.unique_listings)} unique, "
        f"{dedup_result.duplicates_removed} duplicates removed"
    )
    
    # Save unique winners plus flagged duplicates so DB duplicate flags stay fresh.
    saved_count = 0
    duplicate_saved_count = 0
    listings_to_save = dedup_result.unique_listings + dedup_result.duplicate_listings
    pending_count = 0
    pending_saved_count = 0
    pending_duplicate_count = 0
    batch_size = 500

    def reset_pending_counts():
        nonlocal pending_count, pending_saved_count, pending_duplicate_count
        pending_count = 0
        pending_saved_count = 0
        pending_duplicate_count = 0

    for listing_data in listings_to_save:
        try:
            repo.upsert(listing_data, commit=False)
            pending_count += 1
            if listing_data.get('is_duplicate'):
                duplicate_saved_count += 1
                pending_duplicate_count += 1
            else:
                saved_count += 1
                pending_saved_count += 1
            if pending_count >= batch_size:
                db.commit()
                reset_pending_counts()
        except Exception as e:
            logger.error(f"Error saving listing {listing_data.get('source_id')}: {e}")
            db.rollback()
            saved_count -= pending_saved_count
            duplicate_saved_count -= pending_duplicate_count
            reset_pending_counts()

    if pending_count:
        db.commit()
    
    logger.info(
        f"Saved {saved_count} unique listings and "
        f"{duplicate_saved_count} duplicate listings to database"
    )
    
    # Mark inactive listings
    for source, ids in active_source_ids.items():
        active_count = repo.count_active_by_source(source)
        scraped_count = len(ids)
        if active_count > 0:
            ratio = scraped_count / active_count
            if ratio < MARK_INACTIVE_MIN_RATIO:
                message = (
                    f"Skipping mark_inactive for {source}: scraped {scraped_count} "
                    f"listings vs {active_count} active in DB "
                    f"({ratio:.1%} < {MARK_INACTIVE_MIN_RATIO:.0%})"
                )
                logger.warning(message)
                if recorder is not None:
                    recorder.add_error(message)
                continue
        if ids:
            marked = repo.mark_inactive(source, ids)
            logger.info(f"Marked {marked} {source} listings as inactive")

    newly_off_market = repo.mark_stale_inactive_as_sold(SOLD_AFTER_DAYS)
    off_market_count = repo.count_off_market()
    logger.info(
        f"Marked {newly_off_market} listings off market after {SOLD_AFTER_DAYS} days; "
        f"{off_market_count} off market in total"
    )
    if recorder is not None:
        recorder.set_off_market(
            total=off_market_count,
            newly_marked=newly_off_market,
        )
    
    # Update neighborhood stats
    update_neighborhood_stats(db)
    
    # Print deduplication stats
    if dedup_result.duplicates_by_source:
        logger.info("Duplicates by source:")
        for source, count in dedup_result.duplicates_by_source.items():
            logger.info(f"  {source}: {count} duplicates")
    
    return saved_count


def update_neighborhood_stats(db):
    """Update neighborhood statistics."""
    logger.info("Updating neighborhood stats...")
    
    repo = ListingRepository(db)
    hood_repo = NeighborhoodRepository(db)
    history_repo = NeighborhoodStatsHistoryRepository(db)
    
    # Get only unique (non-duplicate) active listings for stats
    listings = repo.get_active()
    unique_listings = [l for l in listings if not getattr(l, 'is_duplicate', False)]
    
    stats = calculate_neighborhood_stats(unique_listings)

    published_stats = select_published_neighborhood_stats(stats)
    for neighborhood, group_stats in published_stats.items():
        hood_repo.update_stats(
            name=neighborhood,
            avg_price=group_stats['mean'],
            median_price=group_stats['median'],
            count=group_stats['count'],
        )

    history_repo.record_snapshot(published_stats)

    # Clear stats for hoods NOT in the published set — otherwise a hood that
    # gets suppressed (e.g. land-dominated, TIN-476) keeps serving its stale
    # numbers from the Neighborhood table forever.
    from src.database.models import Neighborhood as _Neighborhood
    cleared = db.query(_Neighborhood).filter(
        ~_Neighborhood.name.in_(list(published_stats.keys())),
        _Neighborhood.median_price_per_sqm.isnot(None),
    ).update(
        {"avg_price_per_sqm": None, "median_price_per_sqm": None, "listing_count": 0},
        synchronize_session=False,
    )
    db.commit()

    logger.info(
        f"Updated published stats for {len(published_stats)} neighborhoods"
        + (f"; cleared {cleared} suppressed" if cleared else "")
    )


def select_published_neighborhood_stats(stats):
    """Select one stable tier per neighborhood for persisted user-facing stats.

    Fallback order: apartments → houses → all-types. The all-types tier is
    NEVER published for plot-dominated hoods — the imot.bg benchmark exposed
    Нови Искър publishing €173/m² (land prices) vs their €1,624 (TIN-476).
    A plot-heavy blend is detected by comparing against the plot tier.
    """
    neighborhoods = {key[0] for key in stats}
    selected = {}

    for neighborhood in neighborhoods:
        for prop_type in ('apartment', 'house'):
            key = (neighborhood, prop_type, 'all')
            if key in stats:
                selected[neighborhood] = stats[key]
                break
        else:
            fallback = stats.get((neighborhood, 'all', 'all'))
            plots = stats.get((neighborhood, 'plot', 'all'))
            # Publish the mixed tier only when it isn't just land prices:
            # if a plot tier exists and the blend sits below 2× the plot
            # median, land dominates — better to publish nothing.
            if fallback and (
                plots is None or fallback['median'] > 2 * plots['median']
            ):
                selected[neighborhood] = fallback

    # Absolute sanity floor: no residential €/m² in Sofia is genuinely below
    # this — anything under it is a land-dominated blend that slipped past
    # the plot-tier check (Суходол published €177/m² vs imot.bg's €1,787).
    # Publishing nothing beats publishing land prices as home prices.
    return {
        hood: group
        for hood, group in selected.items()
        if group['median'] >= PUBLISH_MIN_MEDIAN_EUR_SQM
    }


def cmd_analyze():
    """Run analysis on stored data."""
    logger.info("Starting analysis...")
    
    db = get_db()
    
    # Detect anomalies
    anomalies = analyze_database(db)
    logger.info(f"Found {len(anomalies)} anomalies")
    
    # Create alerts
    alert_repo = AlertRepository(db)
    created = 0
    
    for anomaly in anomalies:
        # Check if alert already exists
        if not alert_repo.exists_for_listing(anomaly.listing.id, 'underpriced'):
            alert_repo.create(
                listing_id=anomaly.listing.id,
                alert_type='underpriced',
                zscore=anomaly.zscore,
                savings_eur=anomaly.savings_eur,
                savings_pct=anomaly.savings_pct,
            )
            created += 1
    
    logger.info(f"Created {created} new alerts")
    
    # Calculate trends
    trends = calculate_neighborhood_trends(db)
    logger.info(f"Calculated trends for {len(trends)} neighborhoods")
    
    return len(anomalies)


def cmd_alerts():
    """Build a SINGLE Telegram digest message from new alerts.

    Replaces the per-deal blast (which sent ~750 individual messages on first
    run). Now collects all unsent underpriced alerts, applies aggressive
    filters (active listings only, apartments only, sane area, neighborhood
    baseline exists), takes the top 3 by best z-score, and packs them into
    ONE digest with a link to the dashboard for the rest.

    Filters applied (drops alerts that are noise):
      - listing must be `is_active`
      - listing.property_type == 'apartment' (skips parcels/commercial that
        polluted the original deal list with bogus €/m² figures)
      - 30 ≤ area_sqm ≤ 500 (typical apartment range — excludes 4000m² plots
        misclassified as apartments)
      - neighborhood has a non-zero avg_price_per_sqm baseline
      - z-score ≤ -1.5

    All alerts considered (whether they made the top 3 or not) are marked
    sent — they've been "processed" for this run. New deals from the next
    scrape produce new alerts.
    """
    from src.alerts.telegram import format_telegram_digest
    from src.message_sender import send_simple_message
    from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    # Skip cleanly when Telegram isn't configured (TIN-447). Alerts stay
    # unsent so they retry once credentials are provided; the pipeline
    # continues to the dashboard export either way.
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID empty) "
            "— skipping digest; alerts left unsent"
        )
        return {
            'sent': 0, 'qualified': 0, 'considered': 0,
            'top_deals': [], 'by_neighborhood': [],
            'skipped': 'telegram_not_configured',
        }

    logger.info("Building digest from new alerts...")

    db = get_db()
    alert_repo = AlertRepository(db)
    hood_repo = NeighborhoodRepository(db)

    unsent = alert_repo.get_unsent()
    logger.info(f"Found {len(unsent)} unsent alerts to consider")

    # Filter to genuine, sendable underpriced deals.
    qualified: List = []  # list of (alert, hood_avg) tuples
    for alert in unsent:
        if alert.alert_type != 'underpriced' or not alert.listing:
            continue
        listing = alert.listing
        # Active only — no dead 403 listings (this was happening before).
        if not getattr(listing, 'is_active', False):
            continue
        # Apartments only — parcels/commercial pollute €/m² stats.
        if (listing.property_type or '').lower() != 'apartment':
            continue
        # Sane area. Excludes 4000m² "apartments" that are actually plots.
        area = listing.area_sqm or 0
        if area < 30 or area > 500:
            continue
        # Z-score threshold.
        if not should_send_alert(alert.zscore or 0, min_zscore=-1.5):
            continue
        # Neighborhood baseline must exist.
        hood = hood_repo.get_or_create(listing.neighborhood) if listing.neighborhood else None
        hood_avg = getattr(hood, 'avg_price_per_sqm', None) if hood else None
        if not hood_avg or hood_avg <= 0:
            continue
        qualified.append((alert, hood_avg))

    logger.info(f"Qualified after filters: {len(qualified)} of {len(unsent)}")

    # Top 3 by lowest (most negative) z-score = best deals.
    qualified.sort(key=lambda x: x[0].zscore or 0)
    top = qualified[:3]

    top_deals_payload = [
        {
            'neighborhood': a.listing.neighborhood,
            'price_eur': float(a.listing.price_eur),
            'area_sqm': float(a.listing.area_sqm),
            'rooms': a.listing.rooms,
            'price_per_sqm_eur': float(a.listing.price_per_sqm_eur),
            'zscore': float(a.zscore or 0),
            'savings_pct': float(a.savings_pct or 0),
            'url': a.listing.url,
            # When we first saw the listing → digest renders this as "Added Xd ago"
            'first_seen': a.listing.first_seen,
        }
        for a, _ in top
    ]

    # Hottest neighborhoods — count qualified deals per neighborhood, top 3.
    hood_counts: Dict[str, Dict[str, Any]] = {}
    for a, hood_avg in qualified:
        name = a.listing.neighborhood or 'Unknown'
        bucket = hood_counts.setdefault(name, {'count': 0, 'avg': hood_avg})
        bucket['count'] += 1
    by_neighborhood_payload = [
        {'neighborhood': name, 'deal_count': v['count'], 'avg_price_per_sqm': v['avg']}
        for name, v in sorted(hood_counts.items(), key=lambda x: -x[1]['count'])[:3]
    ]

    # Aggregate stats.
    listing_repo = ListingRepository(db)
    total_active = sum(
        1 for l in listing_repo.get_active()
        if not getattr(l, 'is_duplicate', False)
    )

    # Build + send the single digest.
    if not qualified:
        logger.info("No qualified deals — skipping Telegram digest entirely")
        # Still mark all considered alerts as sent so they don't re-queue.
        for alert in unsent:
            alert_repo.mark_sent(alert.id)
        return {
            'sent': 0, 'qualified': 0, 'considered': len(unsent),
            'top_deals': [], 'by_neighborhood': [],
        }

    digest_text = format_telegram_digest(
        top_deals=top_deals_payload,
        total_new_deals=len(qualified),
        total_active_listings=total_active,
        by_neighborhood=by_neighborhood_payload,
    )

    ok = send_simple_message(digest_text)
    # Serialize first_seen so the dict can be JSON-dumped for runs.json.
    serializable_top = []
    for d in top_deals_payload:
        item = dict(d)
        if hasattr(item.get('first_seen'), 'isoformat'):
            item['first_seen'] = item['first_seen'].isoformat()
        serializable_top.append(item)

    if ok:
        # Mark every considered alert sent — top 3 + the rest are now "processed".
        for alert in unsent:
            alert_repo.mark_sent(alert.id)
        logger.info(
            f"Sent digest with top {len(top)} of {len(qualified)} qualified deals; "
            f"marked {len(unsent)} alerts as sent"
        )
        return {
            'sent': 1, 'qualified': len(qualified), 'considered': len(unsent),
            'top_deals': serializable_top, 'by_neighborhood': by_neighborhood_payload,
        }
    else:
        logger.error("Digest send failed; alerts left unsent for retry next run")
        return {
            'sent': 0, 'qualified': len(qualified), 'considered': len(unsent),
            'top_deals': serializable_top, 'by_neighborhood': by_neighborhood_payload,
        }


def cmd_stats():
    """Print database statistics."""
    db = get_db()
    repo = ListingRepository(db)
    
    stats = repo.get_stats()
    
    # Get unique listing count (excluding duplicates)
    all_listings = repo.get_active()
    unique_count = sum(1 for l in all_listings if not getattr(l, 'is_duplicate', False))
    duplicate_count = sum(1 for l in all_listings if getattr(l, 'is_duplicate', False))
    
    print("\n" + "="*60)
    print("DATABASE STATISTICS")
    print("="*60)
    print(f"Total listings: {stats['total_listings']}")
    print(f"Active listings: {stats['active_listings']}")
    print(f"  - Unique: {unique_count}")
    print(f"  - Duplicates: {duplicate_count}")
    
    print("\nBy Source:")
    for source, count in sorted(stats['by_source'].items()):
        print(f"  {source}: {count}")
    
    print("\nTop 10 Neighborhoods:")
    for hood, count in list(stats['top_neighborhoods'].items())[:10]:
        print(f"  {hood}: {count}")
    
    print("="*60)
    
    return stats


def cmd_dedup_stats():
    """Show deduplication statistics without running full scrape."""
    db = get_db()
    repo = ListingRepository(db)
    
    listings = repo.get_active()
    listings_data = [
        {
            'source': l.source,
            'source_id': l.source_id,
            'neighborhood': l.neighborhood,
            'area_sqm': l.area_sqm,
            'price_eur': l.price_eur,
            'rooms': l.rooms,
            'property_type': l.property_type,
        }
        for l in listings
    ]
    
    stats = get_duplicate_stats(listings_data)
    
    print("\n" + "="*60)
    print("DEDUPLICATION STATISTICS")
    print("="*60)
    print(f"Total active listings: {stats['total_listings']}")
    print(f"Unique fingerprints: {stats['unique_fingerprints']}")
    print(f"Duplicate groups: {stats['duplicate_groups']}")
    print(f"Listings in duplicate groups: {stats['duplicate_listings']}")
    
    if stats['source_combinations']:
        print("\nSource overlap (duplicates across sources):")
        for sources, count in sorted(stats['source_combinations'].items(), key=lambda x: -x[1]):
            print(f"  {sources}: {count} properties")
    
    print("="*60)
    
    return stats


def cmd_export_dashboard():
    """Regenerate dashboard JSON files from DB; auto-commit + push if enabled.

    Writes <DASHBOARD_REPO_PATH>/data/dashboard/data.json and daily-digest.json. When
    DASHBOARD_AUTO_PUSH is on (default), commits and pushes those files so
    Vercel re-deploys automatically.
    """
    logger.info("Exporting dashboard data...")
    db = get_db()

    from src.exporters.dashboard import export_dashboard
    summary = export_dashboard(db)

    if not summary.get("ok"):
        logger.error(f"Dashboard export failed: {summary.get('reason')}")
        return summary

    print("\n" + "=" * 60)
    print("DASHBOARD EXPORT")
    print("=" * 60)
    print(f"Listings written:    {summary['listings']}")
    print(f"Deals (z ≤ -1.5):    {summary['deals']}")
    print(f"Neighborhoods:       {summary['neighborhoods']}")
    print(f"Files:               {', '.join(summary['wrote'])}")
    print(f"Pushed to GitHub:    {'yes' if summary['pushed'] else 'no'}")
    print("=" * 60)
    return summary


def cmd_full():
    """Run full pipeline: scrape + analyze + alerts + dashboard export.

    Wrapped in a RunRecorder so per-source stats and timing land in runs.json
    on the dashboard. Status.json is pushed at start and end so the dashboard's
    StatusBadge can show "running" vs "idle" + last summary.
    """
    from src.observability import RunRecorder, write_status, append_run

    logger.info("Running full pipeline...")

    rec = RunRecorder()
    rec.start()

    # Push "running" state up-front so the dashboard knows immediately.
    # Failure here is non-fatal — pipeline keeps going.
    try:
        write_status("running", summary={"started_at": rec.started_at})
    except Exception as e:
        logger.warning(f"Could not push 'running' status: {e}")

    try:
        # Step 1: Scrape (recorder collects per-source results)
        with rec.step("scrape"):
            scraped = cmd_scrape(recorder=rec)

        # Step 2: Analyze (recompute z-scores; produce alerts)
        with rec.step("analyze"):
            anomalies = cmd_analyze()
        rec.set_analysis(anomalies=anomalies, neighborhoods=0, groups_used=0)

        # Step 3: Alerts (Telegram digest — single message per run).
        # Non-fatal by design (TIN-447): a broken/unconfigured Telegram must
        # never block the dashboard export that follows.
        try:
            with rec.step("alerts"):
                alerts_summary = cmd_alerts() or {}
        except Exception as e:
            logger.error(f"Alerts step failed (continuing to export): {e}")
            rec.add_error(f"alerts: {str(e)[:200]}")
            alerts_summary = {}
        rec.set_digest(
            sent=alerts_summary.get('sent', 0),
            qualified=alerts_summary.get('qualified', 0),
            considered=alerts_summary.get('considered', 0),
            top_deals=alerts_summary.get('top_deals', []),
        )

        # Step 4: Refresh dashboard data + auto-deploy via Vercel
        with rec.step("export"):
            export_summary = cmd_export_dashboard()

        # Compute final active count for the run record.
        from src.database.models import get_db as _get_db
        from src.database.repository import ListingRepository as _LRepo
        _db = _get_db()
        active_after = sum(
            1 for l in _LRepo(_db).get_active()
            if not getattr(l, 'is_duplicate', False)
        )
        rec.finalize(active_after=active_after, scraped_total=scraped)
    except Exception as e:
        rec.add_error(f"pipeline crash: {e}")
        rec.finalize(active_after=0, scraped_total=0)
        # Push error status before re-raising
        try:
            write_status("error", summary={"error": str(e)[:300], "run_id": rec.id})
        except Exception:
            pass
        raise

    # Append the completed run record to runs.json (no individual push — we
    # piggyback on the dashboard export's push when possible, but this writes
    # the file in-place; the export step above already pushed.)
    try:
        append_run(rec.to_dict(), push=True)
    except Exception as e:
        logger.warning(f"Could not append run record: {e}")

    # Push final "idle" status with the summary block.
    try:
        write_status("idle", summary={
            "last_run_id": rec.id,
            "last_run_finished_at": rec.finished_at,
            "duration_sec": rec.duration_sec,
            "scraped_total": rec.totals.get("scraped_total", 0),
            "active_after": rec.totals.get("active_after", 0),
            "anomalies": rec.analysis.get("anomalies", 0),
            "digest_sent": rec.digest.get("sent", 0),
            "qualified": rec.digest.get("qualified", 0),
            "status": rec.status,
        })
    except Exception as e:
        logger.warning(f"Could not push 'idle' status: {e}")

    logger.info(
        f"Pipeline complete: {scraped} scraped, {anomalies} anomalies, "
        f"digest_sent={alerts_summary.get('sent', 0)}, "
        f"qualified_deals={alerts_summary.get('qualified', 0)}, "
        f"dashboard_pushed={export_summary.get('pushed', False)}"
    )

    return {
        'scraped': scraped,
        'anomalies': anomalies,
        'alerts': alerts_summary,
        'dashboard': export_summary,
    }


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sofia Real Estate Intelligence Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main scrape            # Run scrapers with deduplication
  python -m src.main analyze           # Analyze listings
  python -m src.main alerts            # Generate and send alerts
  python -m src.main stats             # Show statistics
  python -m src.main dedup-stats       # Show deduplication stats
  python -m src.main export-dashboard  # Refresh dashboard JSON + push to GitHub
  python -m src.main full              # Run full pipeline (scrape→analyze→alerts→export)
        """
    )

    parser.add_argument(
        'command',
        choices=['scrape', 'analyze', 'alerts', 'stats', 'full', 'init', 'dedup-stats', 'export-dashboard'],
        help='Command to run'
    )
    
    args = parser.parse_args()
    
    # Initialize database
    if args.command == 'init':
        logger.info("Initializing database...")
        init_db()
        logger.info("Database initialized!")
        return
    
    # Ensure database is initialized
    init_db()
    
    # Run command
    commands = {
        'scrape': cmd_scrape,
        'analyze': cmd_analyze,
        'alerts': cmd_alerts,
        'stats': cmd_stats,
        'full': cmd_full,
        'dedup-stats': cmd_dedup_stats,
        'export-dashboard': cmd_export_dashboard,
    }
    
    try:
        result = commands[args.command]()
        
        if args.command == 'stats':
            # Already printed
            pass
        elif args.command == 'dedup-stats':
            # Already printed
            pass
        elif args.command == 'alerts':
            # cmd_alerts now returns a digest summary dict
            r = result if isinstance(result, dict) else {}
            sent = r.get('sent', 0)
            qualified = r.get('qualified', 0)
            considered = r.get('considered', 0)
            print(
                f"\nDigest: {sent} message(s) sent · "
                f"{qualified} qualified deal(s) · {considered} alerts considered"
            )
        elif args.command == 'full':
            print(f"\nPipeline complete:")
            print(f"  Scraped: {result['scraped']} listings")
            print(f"  Anomalies: {result['anomalies']}")
            alerts_summary = result.get('alerts')
            if isinstance(alerts_summary, dict):
                print(
                    f"  Alerts: {alerts_summary.get('sent', 0)} digest sent · "
                    f"{alerts_summary.get('qualified', 0)} qualified"
                )
            else:
                print(f"  Alerts: {alerts_summary}")
        else:
            print(f"\nCommand '{args.command}' completed successfully")
            
    except Exception as e:
        logger.error(f"Error running command: {e}")
        raise


if __name__ == '__main__':
    main()
