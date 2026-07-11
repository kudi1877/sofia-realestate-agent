"""Repository layer for database operations."""

from typing import List, Optional, Dict, Any
from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_, desc

from src.database.models import Listing, PriceHistory, Neighborhood, Alert
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
    
    def get_active(self, limit: Optional[int] = None) -> List[Listing]:
        """Get all active listings."""
        query = self.db.query(Listing).filter(Listing.is_active == True)
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
    
    def mark_sold(self, source: str, source_id: str):
        """Mark a listing as sold."""
        listing = self.get_by_source_id(source, source_id)
        if listing:
            listing.is_sold = True
            now = utc_now()
            listing.sold_date = now
            listing.is_active = False
            if listing.first_seen:
                listing.days_on_market = (now - listing.first_seen).days
            self.db.commit()
    
    def get_sold(self, days: int = 30) -> List[Listing]:
        """Get recently sold listings."""
        cutoff = utc_now() - timedelta(days=days)
        return self.db.query(Listing).filter(
            and_(
                Listing.is_sold == True,
                Listing.sold_date >= cutoff,
            )
        ).order_by(desc(Listing.sold_date)).all()
    
    def get_stale(self, days: int = 7) -> List[Listing]:
        """Get listings not seen in N days (potentially sold)."""
        cutoff = utc_now() - timedelta(days=days)
        return self.db.query(Listing).filter(
            and_(
                Listing.is_active == True,
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
        """Get unsent alerts."""
        return self.db.query(Alert).filter(Alert.sent_at.is_(None)).all()
    
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
