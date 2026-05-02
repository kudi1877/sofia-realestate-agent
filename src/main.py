"""Main entry point for Sofia Real Estate Agent."""

import sys
import argparse
from typing import List

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
    """Generate and send alert text for new anomalies via Telegram."""
    logger.info("Generating alerts...")
    
    db = get_db()
    alert_repo = AlertRepository(db)
    
    unsent = alert_repo.get_unsent()
    logger.info(f"Found {len(unsent)} unsent alerts")
    
    messages = []
    sent_count = 0
    
    hood_repo = NeighborhoodRepository(db)

    for alert in unsent:
        if alert.alert_type == 'underpriced' and alert.listing:
            # Check if meets threshold (zscore < -1.5)
            if not should_send_alert(alert.zscore or 0, min_zscore=-1.5):
                logger.debug(f"Skipping alert for listing {alert.listing.id} (zscore={alert.zscore})")
                continue

            # 0-baseline edge case: if neighborhood has no avg_price_per_sqm, the
            # "savings" figure in the alert is meaningless. Skip rather than send
            # a misleading "0% below avg" message.
            hood = hood_repo.get_or_create(alert.listing.neighborhood) if alert.listing.neighborhood else None
            hood_avg = getattr(hood, 'avg_price_per_sqm', None) if hood else None
            if not hood_avg or hood_avg <= 0:
                logger.warning(
                    f"Skipping alert {alert.id} ({alert.listing.neighborhood}): "
                    f"neighborhood baseline missing or zero — savings would be misleading"
                )
                continue

            # Convert listing to alert format
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
            
            # Format message
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
    
    # Send alerts via Telegram Bot API
    from src.message_sender import send_deal_alerts

    if messages:
        sent_ids = send_deal_alerts(messages)
        sent_count = len(sent_ids)
        for alert_id in sent_ids:
            alert_repo.mark_sent(alert_id)
        logger.info(
            f"Sent {sent_count}/{len(messages)} deal alerts via Telegram "
            f"(unsent will be retried next run)"
        )

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


def cmd_full():
    """Run full pipeline: scrape + analyze + alerts."""
    logger.info("Running full pipeline...")
    
    # Step 1: Scrape
    scraped = cmd_scrape()
    
    # Step 2: Analyze
    anomalies = cmd_analyze()
    
    # Step 3: Alerts
    messages = cmd_alerts()
    
    logger.info(f"Pipeline complete: {scraped} scraped, {anomalies} anomalies, {len(messages)} alerts")
    
    return {
        'scraped': scraped,
        'anomalies': anomalies,
        'alerts': len(messages),
    }


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sofia Real Estate Intelligence Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main scrape       # Run scrapers with deduplication
  python -m src.main analyze      # Analyze listings
  python -m src.main alerts       # Generate and send alerts
  python -m src.main stats        # Show statistics
  python -m src.main dedup-stats  # Show deduplication stats
  python -m src.main full         # Run full pipeline
        """
    )
    
    parser.add_argument(
        'command',
        choices=['scrape', 'analyze', 'alerts', 'stats', 'full', 'init', 'dedup-stats'],
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
            print(f"\nGenerated {len(result)} alerts")
        elif args.command == 'full':
            print(f"\nPipeline complete:")
            print(f"  Scraped: {result['scraped']} listings")
            print(f"  Anomalies: {result['anomalies']}")
            print(f"  Alerts: {result['alerts']}")
        else:
            print(f"\nCommand '{args.command}' completed successfully")
            
    except Exception as e:
        logger.error(f"Error running command: {e}")
        raise


if __name__ == '__main__':
    main()
