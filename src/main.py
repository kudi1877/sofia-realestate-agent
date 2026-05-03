"""Main entry point for Sofia Real Estate Agent."""

import sys
import argparse
from typing import Any, Dict, List

from loguru import logger

from src.database.models import init_db, get_db
from src.database.repository import ListingRepository, AlertRepository, NeighborhoodRepository
from src.scrapers.imotbg import ImotBgScraper
from src.scrapers.homesbg import HomesBgScraper
from src.scrapers.imotiinfo import ImotiInfoScraper
from src.scrapers.imotinet import ImotiNetScraper
from src.scrapers.propertybg import PropertyBGScraper
from src.analysis.anomaly import analyze_database, calculate_neighborhood_stats
from src.analysis.trends import calculate_neighborhood_trends, generate_market_summary
from src.alerts.telegram import format_deal_alert, format_simple_alert, should_send_alert, listing_to_alert
from src.utils.deduplication import deduplicate_listings, get_duplicate_stats


# Configure logging
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


def cmd_scrape():
    """Run all scrapers with deduplication."""
    logger.info("Starting scraping...")
    
    db = get_db()
    repo = ListingRepository(db)
    
    all_listings = []
    active_source_ids = {}
    
    # Scrape imot.bg
    try:
        with ImotBgScraper() as scraper:
            listings = scraper.scrape()
            logger.info(f"imot.bg: scraped {len(listings)} listings")
            all_listings.extend(listings)
            active_source_ids['imotbg'] = [l['source_id'] for l in listings]
    except Exception as e:
        logger.error(f"Error scraping imot.bg: {e}")
    
    # Scrape homes.bg (API-based, no context manager needed)
    try:
        scraper = HomesBgScraper()
        listings = scraper.scrape()
        logger.info(f"homes.bg: scraped {len(listings)} listings")
        all_listings.extend(listings)
        active_source_ids['homesbg'] = [l['source_id'] for l in listings]
    except Exception as e:
        logger.error(f"Error scraping homes.bg: {e}")
    
    # Scrape imoti.info
    try:
        with ImotiInfoScraper() as scraper:
            listings = scraper.scrape()
            logger.info(f"imoti.info: scraped {len(listings)} listings")
            all_listings.extend(listings)
            active_source_ids['imotiinfo'] = [l['source_id'] for l in listings]
    except Exception as e:
        logger.error(f"Error scraping imoti.info: {e}")
    
    # Scrape imoti.net
    try:
        scraper = ImotiNetScraper()
        listings = scraper.scrape()
        logger.info(f"imoti.net: scraped {len(listings)} listings")
        all_listings.extend(listings)
        active_source_ids['imotinet'] = [l['source_id'] for l in listings]
    except Exception as e:
        logger.error(f"Error scraping imoti.net: {e}")
    
    # Scrape property.bg
    try:
        scraper = PropertyBGScraper()
        listings = scraper.scrape()
        logger.info(f"property.bg: scraped {len(listings)} listings")
        all_listings.extend(listings)
        active_source_ids['propertybg'] = [l['source_id'] for l in listings]
    except Exception as e:
        logger.error(f"Error scraping property.bg: {e}")
    
    # Deduplicate listings before saving
    logger.info(f"Total raw listings: {len(all_listings)}")
    dedup_result = deduplicate_listings(all_listings)
    logger.info(
        f"After deduplication: {len(dedup_result.unique_listings)} unique, "
        f"{dedup_result.duplicates_removed} duplicates removed"
    )
    
    # Save unique listings to database
    saved_count = 0
    for listing_data in dedup_result.unique_listings:
        try:
            repo.upsert(listing_data)
            saved_count += 1
        except Exception as e:
            logger.error(f"Error saving listing {listing_data.get('source_id')}: {e}")
    
    logger.info(f"Saved {saved_count} unique listings to database")
    
    # Mark inactive listings
    for source, ids in active_source_ids.items():
        if ids:
            marked = repo.mark_inactive(source, ids)
            logger.info(f"Marked {marked} {source} listings as inactive")
    
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
    
    # Get only unique (non-duplicate) active listings for stats
    listings = repo.get_active()
    unique_listings = [l for l in listings if not getattr(l, 'is_duplicate', False)]
    
    stats = calculate_neighborhood_stats(unique_listings)
    
    for key, group_stats in stats.items():
        neighborhood = key[0]
        hood_repo.update_stats(
            name=neighborhood,
            avg_price=group_stats['mean'],
            median_price=group_stats['median'],
            count=group_stats['count'],
        )
    
    logger.info(f"Updated stats for {len(stats)} neighborhood groups")


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
        return {'sent': 0, 'qualified': 0, 'considered': len(unsent)}

    digest_text = format_telegram_digest(
        top_deals=top_deals_payload,
        total_new_deals=len(qualified),
        total_active_listings=total_active,
        by_neighborhood=by_neighborhood_payload,
    )

    ok = send_simple_message(digest_text)
    if ok:
        # Mark every considered alert sent — top 3 + the rest are now "processed".
        for alert in unsent:
            alert_repo.mark_sent(alert.id)
        logger.info(
            f"Sent digest with top {len(top)} of {len(qualified)} qualified deals; "
            f"marked {len(unsent)} alerts as sent"
        )
        return {'sent': 1, 'qualified': len(qualified), 'considered': len(unsent)}
    else:
        logger.error("Digest send failed; alerts left unsent for retry next run")
        return {'sent': 0, 'qualified': len(qualified), 'considered': len(unsent)}


def _legacy_per_deal_alerts_DISABLED():
    """Old per-deal alert blaster. Kept here as a reference for what NOT to
    do — sending one Telegram message per anomaly produced 750+ messages on
    the first DB seed. The active path is `cmd_alerts` above.
    """
    db = get_db()
    alert_repo = AlertRepository(db)
    unsent = alert_repo.get_unsent()
    messages = []
    hood_repo = NeighborhoodRepository(db)
    for alert in unsent:
        if alert.alert_type == 'underpriced' and alert.listing:
            if not should_send_alert(alert.zscore or 0, min_zscore=-1.5):
                continue
            hood = hood_repo.get_or_create(alert.listing.neighborhood) if alert.listing.neighborhood else None
            hood_avg = getattr(hood, 'avg_price_per_sqm', None) if hood else None
            if not hood_avg or hood_avg <= 0:
                continue
            listing_data = {
                'id': alert.listing.id,
                'neighborhood': alert.listing.neighborhood,
                'price_eur': alert.listing.price_eur,
                'area_sqm': alert.listing.area_sqm,
                'price_per_sqm_eur': alert.listing.price_per_sqm_eur,
                'rooms': alert.listing.rooms,
                'property_type': alert.listing.property_type,
                'url': alert.listing.url,
                'source': alert.listing.source,
            }
            deal_alert = listing_to_alert(
                listing_data,
                zscore=alert.zscore or 0,
                savings_pct=alert.savings_pct or 0,
                savings_eur=alert.savings_eur or 0,
            )
            message = format_deal_alert(deal_alert)
            messages.append({
                'alert_id': alert.id,
                'message': message,
                'simple_message': format_simple_alert(
                    neighborhood=alert.listing.neighborhood,
                    price_eur=alert.listing.price_eur,
                    savings_pct=alert.savings_pct or 0,
                    url=alert.listing.url,
                    rooms=alert.listing.rooms,
                    area_sqm=alert.listing.area_sqm,
                ),
                'listing': alert.listing,
                'zscore': alert.zscore,
            })
    from src.message_sender import send_deal_alerts
    if messages:
        sent_ids = send_deal_alerts(messages)
        for alert_id in sent_ids:
            alert_repo.mark_sent(alert_id)
    return messages


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

    Writes <DASHBOARD_REPO_PATH>/public/data.json and daily-digest.json. When
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
    """Run full pipeline: scrape + analyze + alerts + dashboard export."""
    logger.info("Running full pipeline...")

    # Step 1: Scrape
    scraped = cmd_scrape()

    # Step 2: Analyze
    anomalies = cmd_analyze()

    # Step 3: Alerts (Telegram digest — single message per run)
    alerts_summary = cmd_alerts() or {}

    # Step 4: Refresh dashboard data + auto-deploy via Vercel
    export_summary = cmd_export_dashboard()

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
