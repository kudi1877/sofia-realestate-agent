"""Repository layer for database operations."""

import json
from typing import List, Optional, Dict, Any
from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_, desc

from src.database.models import (
    Alert,
    Listing,
    Neighborhood,
    NeighborhoodStatsHistory,
    PriceHistory,
)
from src.utils.time import utc_now


class ListingRepository:
    """Repository for Listing operations."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_by_source_id(self, source: str, source_id: str) -> Optional[Listing]:
        """Get listing by source and source_id."""
        return self.db.query(Listing).filter(
            and_(Listing.source == source, Listing.source_id == source_id)
        ).first()
    
    def upsert(self, listing_data: Dict[str, Any], commit: bool = True) -> Listing:
        """Insert or update a listing."""
        # Drop keys that aren't Listing attributes — dedup attaches metadata
        # like 'duplicate_sources' to winner dicts, and Listing(**data) raises
        # TypeError on unknown kwargs (killed ~all new-listing saves on the
        # 2026-07-12 nightly run; only 30 of 7,730 scraped rows saved).
        # Also JSON-encode list/dict values: Text columns like image_urls
        # receive Python lists from scrapers, and SQLite can't bind those
        # ("type 'list' is not supported" — same run, same lesson).
        listing_data = {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
            for k, v in listing_data.items()
            if hasattr(Listing, k)
        }

        existing = self.get_by_source_id(
            listing_data["source"],
            listing_data["source_id"]
        )
        
        if existing:
            # Check if price changed
            if existing.price_eur != listing_data["price_eur"]:
                # Record old price in history
                price_history = PriceHistory(
                    listing_id=existing.id,
                    price_eur=existing.price_eur,
                    price_per_sqm_eur=existing.price_per_sqm_eur,
                )
                self.db.add(price_history)
                
                # Track price changes count
                existing.price_changes = (existing.price_changes or 0) + 1
            
            # Update fields
            for key, value in listing_data.items():
                if hasattr(existing, key) and key not in ('first_price_eur', 'price_changes', 'is_sold', 'sold_date', 'days_on_market'):
                    setattr(existing, key, value)
            
            now = utc_now()
            existing.last_seen = now
            existing.is_active = True
            
            # Update days on market
            if existing.first_seen:
                existing.days_on_market = (now - existing.first_seen).days
            
            if commit:
                self.db.commit()
                self.db.refresh(existing)
            return existing
        else:
            # Create new listing
            listing = Listing(**listing_data)
            listing.first_seen = utc_now()
            listing.first_price_eur = listing_data["price_eur"]
            listing.price_changes = 0
            listing.days_on_market = 0
            self.db.add(listing)
            if commit:
                self.db.commit()
                self.db.refresh(listing)
            else:
                self.db.flush()
            
            # Record initial price
            price_history = PriceHistory(
                listing_id=listing.id,
                price_eur=listing.price_eur,
                price_per_sqm_eur=listing.price_per_sqm_eur,
            )
            self.db.add(price_history)
            if commit:
                self.db.commit()
            
            return listing
    
    def get_active(
        self,
        limit: Optional[int] = None,
        listing_kind: Optional[str] = None,
    ) -> List[Listing]:
        """Get active listings, optionally isolated to sale or rent inventory."""
        query = self.db.query(Listing).filter(Listing.is_active == True)
        if listing_kind:
            query = query.filter(Listing.listing_kind == listing_kind)
        if limit:
            query = query.limit(limit)
        return query.all()
    
    def get_by_neighborhood(
        self, 
        neighborhood: str, 
        property_type: Optional[str] = None
    ) -> List[Listing]:
        """Get listings by neighborhood."""
        query = self.db.query(Listing).filter(Listing.neighborhood == neighborhood)
        if property_type:
            query = query.filter(Listing.property_type == property_type)
        return query.all()
    
    def mark_inactive(self, source: str, active_ids: List[str]) -> int:
        """Mark currently-active listings as inactive if not in active_ids.

        Only touches rows that are still active (TIN-448) — repeat sweeps must
        not churn already-inactive rows, and the returned count then means
        "newly deactivated this run".
        """
        result = self.db.query(Listing).filter(
            and_(
                Listing.source == source,
                Listing.is_active == True,
                ~Listing.source_id.in_(active_ids)
            )
        ).update({"is_active": False}, synchronize_session=False)
        self.db.commit()
        return result

    def count_active_by_source(self, source: str) -> int:
        """Count active listings for a source."""
        return self.db.query(func.count(Listing.id)).filter(
            and_(
                Listing.source == source,
                Listing.is_active == True,
            )
        ).scalar() or 0
    
    def get_price_history(self, listing_id: int) -> List[PriceHistory]:
        """Get price history for a listing, ordered by date."""
        return self.db.query(PriceHistory).filter(
            PriceHistory.listing_id == listing_id
        ).order_by(PriceHistory.recorded_at.desc()).all()
    
    def get_price_drops(self, min_drop_pct: float = 5.0) -> List[Listing]:
        """Get listings with significant price drops from first price."""
        listings = self.db.query(Listing).filter(
            and_(
                Listing.is_active == True,
                Listing.listing_kind == "sale",
                or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
                Listing.first_price_eur.isnot(None),
                Listing.price_changes > 0,
            )
        ).all()
        
        drops = []
        for listing in listings:
            if listing.first_price_eur and listing.first_price_eur > 0:
                drop_pct = ((listing.first_price_eur - listing.price_eur) / listing.first_price_eur) * 100
                if drop_pct >= min_drop_pct:
                    drops.append(listing)
        
        return sorted(drops, key=lambda l: (l.first_price_eur - l.price_eur) / l.first_price_eur if l.first_price_eur else 0, reverse=True)
    
    @staticmethod
    def _apply_sold_state(listing: Listing, sold_at) -> None:
        listing.is_sold = True
        listing.sold_date = sold_at
        listing.is_active = False
        if listing.first_seen:
            listing.days_on_market = max(0, (sold_at - listing.first_seen).days)

    def mark_sold(self, source: str, source_id: str, commit: bool = True) -> bool:
        """Mark a listing as sold."""
        listing = self.get_by_source_id(source, source_id)
        if not listing:
            return False

        self._apply_sold_state(listing, utc_now())
        if commit:
            self.db.commit()
        return True

    def mark_stale_inactive_as_sold(self, days: int) -> int:
        """Mark inactive listings unseen for longer than ``days`` off market."""
        now = utc_now()
        cutoff = now - timedelta(days=days)
        stale = self.db.query(Listing).filter(
            Listing.listing_kind == "sale",
            Listing.is_active.is_(False),
            (Listing.is_sold.is_(False)) | (Listing.is_sold.is_(None)),
            Listing.last_seen.isnot(None),
            Listing.last_seen < cutoff,
        ).all()

        for listing in stale:
            self._apply_sold_state(listing, now)
        self.db.commit()
        return len(stale)

    def expire_auctions(self, now=None) -> int:
        """Deactivate auctions once their published bidding window closes."""
        now = now or utc_now()
        result = self.db.query(Listing).filter(
            Listing.listing_kind == "auction",
            Listing.is_active.is_(True),
            Listing.auction_end.isnot(None),
            Listing.auction_end < now,
        ).update({"is_active": False}, synchronize_session=False)
        self.db.commit()
        return result

    def count_off_market(self) -> int:
        """Count unique listings inferred to have left the market."""
        return self.db.query(func.count(Listing.id)).filter(
            Listing.listing_kind == "sale",
            Listing.is_sold.is_(True),
            (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
        ).scalar() or 0
    
    def get_sold(self, days: int = 30) -> List[Listing]:
        """Get recently sold listings."""
        cutoff = utc_now() - timedelta(days=days)
        return self.db.query(Listing).filter(
            and_(
                Listing.is_sold == True,
                Listing.listing_kind == "sale",
                Listing.sold_date >= cutoff,
            )
        ).order_by(desc(Listing.sold_date)).all()
    
    def get_stale(self, days: int = 7) -> List[Listing]:
        """Get listings not seen in N days (potentially sold)."""
        cutoff = utc_now() - timedelta(days=days)
        return self.db.query(Listing).filter(
            and_(
                Listing.is_active == True,
                Listing.listing_kind == "sale",
                Listing.last_seen < cutoff,
            )
        ).all()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        total = self.db.query(func.count(Listing.id)).scalar()
        active = self.db.query(func.count(Listing.id)).filter(Listing.is_active == True).scalar()
        
        by_source = self.db.query(
            Listing.source,
            func.count(Listing.id)
        ).group_by(Listing.source).all()
        
        by_neighborhood = self.db.query(
            Listing.neighborhood,
            func.count(Listing.id)
        ).group_by(Listing.neighborhood).order_by(func.count(Listing.id).desc()).limit(10).all()
        
        return {
            "total_listings": total,
            "active_listings": active,
            "by_source": {s: c for s, c in by_source},
            "top_neighborhoods": {n: c for n, c in by_neighborhood},
        }


class NeighborhoodRepository:
    """Repository for Neighborhood operations."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_or_create(self, name: str) -> Neighborhood:
        """Get or create neighborhood."""
        neighborhood = self.db.query(Neighborhood).filter(Neighborhood.name == name).first()
        if not neighborhood:
            neighborhood = Neighborhood(name=name)
            self.db.add(neighborhood)
            self.db.commit()
            self.db.refresh(neighborhood)
        return neighborhood
    
    def update_stats(
        self, 
        name: str, 
        avg_price: float, 
        median_price: float, 
        count: int
    ):
        """Update neighborhood statistics."""
        neighborhood = self.get_or_create(name)
        neighborhood.avg_price_per_sqm = avg_price
        neighborhood.median_price_per_sqm = median_price
        neighborhood.listing_count = count
        neighborhood.updated_at = utc_now()
        self.db.commit()
    
    def get_all(self) -> List[Neighborhood]:
        """Get all neighborhoods."""
        return self.db.query(Neighborhood).all()


class NeighborhoodStatsHistoryRepository:
    """Repository for per-run neighborhood statistics snapshots."""

    def __init__(self, db: Session):
        self.db = db

    def record_snapshot(
        self,
        stats: Dict[str, Dict[str, float]],
        snapshot_date=None,
    ) -> List[NeighborhoodStatsHistory]:
        """Write exactly one row per published neighborhood for this run."""
        recorded_at = snapshot_date or utc_now()
        rows = [
            NeighborhoodStatsHistory(
                neighborhood=neighborhood,
                snapshot_date=recorded_at,
                median_price_per_sqm=group_stats["median"],
                mean_price_per_sqm=group_stats["mean"],
                listing_count=group_stats["count"],
            )
            for neighborhood, group_stats in stats.items()
        ]
        self.db.add_all(rows)
        self.db.commit()
        return rows


class AlertRepository:
    """Repository for Alert operations."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create(
        self,
        listing_id: int,
        alert_type: str,
        zscore: float,
        savings_eur: float,
        savings_pct: float,
    ) -> Alert:
        """Create a new alert."""
        alert = Alert(
            listing_id=listing_id,
            alert_type=alert_type,
            zscore=zscore,
            savings_eur=savings_eur,
            savings_pct=savings_pct,
        )
        self.db.add(alert)
        self.db.commit()
        self.db.refresh(alert)
        return alert
    
    def get_unsent(self) -> List[Alert]:
        """Get unsent sale alerts; rentals never enter the deal digest."""
        return self.db.query(Alert).join(Listing).filter(
            Alert.sent_at.is_(None),
            Listing.listing_kind == "sale",
        ).all()
    
    def mark_sent(self, alert_id: int):
        """Mark alert as sent."""
        alert = self.db.query(Alert).filter(Alert.id == alert_id).first()
        if alert:
            alert.sent_at = utc_now()
            self.db.commit()
    
    def exists_for_listing(self, listing_id: int, alert_type: str) -> bool:
        """Check if alert already exists for listing."""
        return self.db.query(Alert).filter(
            and_(Alert.listing_id == listing_id, Alert.alert_type == alert_type)
        ).first() is not None
