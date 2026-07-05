from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base
from src.database.repository import ListingRepository


def listing_data(**overrides):
    base = {
        "source": "imotbg",
        "source_id": "flip-1",
        "url": "https://example.test/flip-1",
        "title": "Flag flip listing",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "rooms": 2,
        "area_sqm": 50,
        "price_eur": 100000,
        "price_per_sqm_eur": 2000,
        "canonical_id": "canonical-1",
        "is_duplicate": False,
        "duplicate_of": None,
    }
    base.update(overrides)
    return base


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_upsert_refreshes_duplicate_flags_and_price_on_existing_listing():
    db = session()
    repo = ListingRepository(db)

    repo.upsert(listing_data())
    duplicate = repo.upsert(
        listing_data(price_eur=95000, price_per_sqm_eur=1900, is_duplicate=True, duplicate_of="winner-1")
    )

    assert duplicate.price_eur == 95000
    assert duplicate.price_per_sqm_eur == 1900
    assert duplicate.is_duplicate is True
    assert duplicate.duplicate_of == "winner-1"
    assert duplicate.last_seen is not None

    unique_again = repo.upsert(
        listing_data(price_eur=94000, price_per_sqm_eur=1880, is_duplicate=False, duplicate_of=None)
    )

    assert unique_again.is_duplicate is False
    assert unique_again.duplicate_of is None
    assert unique_again.price_eur == 94000
