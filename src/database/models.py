"""SQLAlchemy database models for Sofia Real Estate Agent."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Float,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import func

from src.config import DATABASE_URL

Base = declarative_base()


class Listing(Base):
    """Real estate listing model."""
    
    __tablename__ = "listings"
    
    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False, index=True)  # imotbg, homesbg
    source_id = Column(String(100), nullable=False)
    listing_kind = Column(String(8), nullable=False, default="sale", server_default="sale", index=True)
    
    # Cross-source deduplication
    canonical_id = Column(String(16), index=True)  # Fingerprint-based unique ID
    is_duplicate = Column(Boolean, default=False)  # Marked as duplicate of another listing
    duplicate_of = Column(String(100))  # source_id of the primary listing
    
    url = Column(Text, nullable=False)
    image_url = Column(Text)
    title = Column(Text)
    
    # Pricing
    price_bgn = Column(Float)
    price_eur = Column(Float, nullable=False)
    area_sqm = Column(Float, nullable=False)
    price_per_sqm_eur = Column(Float, nullable=False, index=True)
    
    # Location
    neighborhood = Column(String(100), nullable=False, index=True)
    
    # Property details
    property_type = Column(String(20), nullable=False, index=True)  # apartment, plot, house
    rooms = Column(Integer)
    floor = Column(Integer)
    total_floors = Column(Integer)
    construction_type = Column(String(20))  # brick, panel, epk
    year_built = Column(Integer)
    furnishing = Column(String(30))  # furnished, partial, unfurnished
    heating = Column(String(30))  # central, local, electric, gas
    
    # Description
    description = Column(Text)
    description_full = Column(Text)
    address = Column(Text)
    latitude = Column(Float)
    longitude = Column(Float)
    seller_type = Column(String(20))
    seller_name = Column(Text)
    contact_phone = Column(String(32))
    contact_email = Column(Text)
    image_urls = Column(Text)
    image_count = Column(Integer)
    enriched_at = Column(DateTime)
    exposure = Column(Text)
    renovation_state = Column(String(30))
    act16 = Column(Boolean)
    has_elevator = Column(Boolean)
    parking = Column(String(30))
    llm_extract = Column(Text)
    llm_extracted_at = Column(DateTime)
    llm_model_used = Column(String(100))
    
    # Price tracking
    first_price_eur = Column(Float)
    price_changes = Column(Integer, default=0)
    is_sold = Column(Boolean, default=False)
    sold_date = Column(DateTime)
    days_on_market = Column(Integer)
    motivated_score = Column(Integer)
    
    # Metadata
    first_seen = Column(DateTime, default=func.now())
    # NO onupdate here (TIN-448): the column-level onupdate fired on the
    # nightly mark_inactive bulk UPDATE too, stamping ~26k dead rows "seen
    # today" so the 30-day soft-deprecation never expired anything. upsert()
    # sets last_seen explicitly on every real re-scrape — the only writer.
    last_seen = Column(DateTime, default=func.now())
    is_active = Column(Boolean, default=True)
    availability_checked_at = Column(DateTime)
    
    # Relationships
    price_history = relationship("PriceHistory", back_populates="listing", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="listing", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index("idx_listing_source_source_id", "source", "source_id", unique=True),
        Index("idx_listing_neighborhood_type", "neighborhood", "property_type"),
        Index("idx_listing_canonical", "canonical_id"),
    )
    
    def __repr__(self):
        return f"<Listing({self.source}:{self.source_id} €{self.price_eur:,.0f})>"


class PriceHistory(Base):
    """Price history for listings."""
    
    __tablename__ = "price_history"
    
    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False, index=True)
    price_eur = Column(Float, nullable=False)
    price_per_sqm_eur = Column(Float, nullable=False)
    recorded_at = Column(DateTime, default=func.now())
    
    # Relationship
    listing = relationship("Listing", back_populates="price_history")
    
    def __repr__(self):
        return f"<PriceHistory(listing_id={self.listing_id} €{self.price_eur:,.0f})>"


class Neighborhood(Base):
    """Neighborhood statistics."""
    
    __tablename__ = "neighborhoods"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    name_bg = Column(String(100))
    zone = Column(String(20))  # center, south, east, north, west
    
    # Statistics
    avg_price_per_sqm = Column(Float)
    median_price_per_sqm = Column(Float)
    listing_count = Column(Integer, default=0)
    
    # Unique listings count (after deduplication)
    unique_listing_count = Column(Integer, default=0)
    
    # Metadata
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    def __repr__(self):
        return f"<Neighborhood({self.name} €{self.avg_price_per_sqm or 0:.0f}/m²)>"


class NeighborhoodStatsHistory(Base):
    """Per-run neighborhood price snapshot used for like-for-like trends."""

    __tablename__ = "neighborhood_stats_history"

    id = Column(Integer, primary_key=True)
    neighborhood = Column(String(100), nullable=False)
    snapshot_date = Column(DateTime, nullable=False, default=func.now())
    median_price_per_sqm = Column(Float, nullable=False)
    mean_price_per_sqm = Column(Float, nullable=False)
    listing_count = Column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "idx_neighborhood_stats_history_lookup",
            "neighborhood",
            "snapshot_date",
        ),
    )


class NeighborhoodRentStats(Base):
    """Current deduplicated rental medians by neighborhood and room bucket."""

    __tablename__ = "neighborhood_rent_stats"

    id = Column(Integer, primary_key=True)
    neighborhood = Column(String(100), nullable=False)
    rooms_bucket = Column(String(8), nullable=False)
    median_rent_per_sqm = Column(Float, nullable=False)
    listing_count = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=func.now())

    __table_args__ = (
        Index(
            "idx_neighborhood_rent_stats_lookup",
            "neighborhood",
            "rooms_bucket",
            unique=True,
        ),
    )


class NeighborhoodRentStatsHistory(Base):
    """Per-run rental median snapshots for later trend analysis."""

    __tablename__ = "neighborhood_rent_stats_history"

    id = Column(Integer, primary_key=True)
    neighborhood = Column(String(100), nullable=False)
    rooms_bucket = Column(String(8), nullable=False)
    snapshot_date = Column(DateTime, nullable=False, default=func.now())
    median_rent_per_sqm = Column(Float, nullable=False)
    listing_count = Column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "idx_neighborhood_rent_stats_history_lookup",
            "neighborhood",
            "rooms_bucket",
            "snapshot_date",
        ),
    )


class Alert(Base):
    """Alert tracking for deals."""
    
    __tablename__ = "alerts"
    
    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False, index=True)
    alert_type = Column(String(20), nullable=False)  # underpriced, price_drop, new_listing
    
    # Analysis
    zscore = Column(Float)
    savings_eur = Column(Float)
    savings_pct = Column(Float)
    
    # Status
    sent_at = Column(DateTime)
    dismissed = Column(Boolean, default=False)
    
    # Relationship
    listing = relationship("Listing", back_populates="alerts")
    
    def __repr__(self):
        return f"<Alert({self.alert_type} listing_id={self.listing_id})>"


# Database engine and session
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database tables and apply lightweight in-place migrations.

    create_all() only creates missing TABLES — it does not add columns to
    existing ones. We apply additive ALTER TABLE migrations here for SQLite
    to keep the model and DB in sync without requiring a full alembic setup.
    Each migration is idempotent: it inspects the current schema first.
    """
    from sqlalchemy import inspect, text

    # create_all is also the idempotent additive migration for new tables such
    # as neighborhood_stats_history; column additions are handled below.
    Base.metadata.create_all(bind=engine)

    # Idempotent column additions for older DBs.
    additive_migrations = {
        "neighborhoods": [
            ("unique_listing_count", "INTEGER DEFAULT 0"),
        ],
        "listings": [
            ("listing_kind", "VARCHAR(8) NOT NULL DEFAULT 'sale'"),
            ("canonical_id", "VARCHAR(16)"),
            ("is_duplicate", "BOOLEAN DEFAULT 0"),
            ("duplicate_of", "VARCHAR(100)"),
            ("first_price_eur", "FLOAT"),
            ("price_changes", "INTEGER DEFAULT 0"),
            ("is_sold", "BOOLEAN DEFAULT 0"),
            ("sold_date", "DATETIME"),
            ("days_on_market", "INTEGER"),
            ("image_url", "TEXT"),
            ("availability_checked_at", "DATETIME"),
            ("motivated_score", "INTEGER"),
            ("description_full", "TEXT"),
            ("address", "TEXT"),
            ("latitude", "FLOAT"),
            ("longitude", "FLOAT"),
            ("seller_type", "VARCHAR(20)"),
            ("seller_name", "TEXT"),
            ("contact_phone", "VARCHAR(32)"),
            ("contact_email", "TEXT"),
            ("image_urls", "TEXT"),
            ("image_count", "INTEGER"),
            ("enriched_at", "DATETIME"),
            ("exposure", "TEXT"),
            ("renovation_state", "VARCHAR(30)"),
            ("act16", "BOOLEAN"),
            ("has_elevator", "BOOLEAN"),
            ("parking", "VARCHAR(30)"),
            ("llm_extract", "TEXT"),
            ("llm_extracted_at", "DATETIME"),
            ("llm_model_used", "VARCHAR(100)"),
        ],
    }

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols in additive_migrations.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_def in cols:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))


def get_db():
    """Get database session."""
    return SessionLocal()
