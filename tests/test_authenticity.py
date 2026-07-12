import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis.authenticity import (
    find_photo_reuse,
    lowest_scorers,
    score_authenticity,
    shingled_description_hash,
)
from src.database.models import Alert, Base, Listing


NOW = datetime(2026, 7, 12)


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, **overrides) -> Listing:
    values = {
        "source": "test",
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "listing_kind": "sale",
        "canonical_id": f"canonical-{source_id}",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "rooms": 2,
        "floor": 3,
        "year_built": 2005,
        "area_sqm": 60,
        "price_eur": 120000,
        "price_per_sqm_eur": 2000,
        "image_url": f"https://images.test/{source_id}.jpg",
        "image_count": 5,
        "first_seen": NOW,
        "last_seen": NOW,
        "is_active": True,
        "is_duplicate": False,
    }
    values.update(overrides)
    return Listing(**values)


def test_photo_reuse_ignores_same_canonical_but_flags_different_properties():
    primary = listing("primary", canonical_id="same", image_phash="0123456789abcdef")
    sibling = listing(
        "sibling",
        canonical_id="same",
        neighborhood="Младост",
        image_phash="0123456789abcdee",
    )
    bait = listing(
        "bait",
        canonical_id="different",
        neighborhood="Лозенец",
        price_eur=400000,
        image_phash="0123456789abcdec",
    )

    conflicts = find_photo_reuse([primary, sibling, bait])

    assert sibling not in conflicts.get(id(primary), [])
    assert bait in conflicts[id(primary)]
    assert primary in conflicts[id(bait)]


def test_shingled_description_hash_is_stable_for_whitespace_and_case():
    description = "Spacious sunny apartment with two bedrooms near metro and a quiet green park today"

    assert shingled_description_hash(description) == shingled_description_hash(description.upper().replace(" ", "  "))


def test_composite_score_persists_evidence_and_review_rows():
    db = session()
    description = "Spacious renovated apartment with two bedrooms near metro station and quiet green park with parking"
    suspicious = listing(
        "suspicious-new",
        canonical_id="suspicious-new",
        neighborhood="Лозенец",
        price_eur=60000,
        price_per_sqm_eur=1000,
        residual_pct=-45,
        image_phash="aaaaaaaaaaaaaaaa",
        image_count=1,
        floor=None,
        year_built=None,
        contact_phone="+359888000000",
        seller_type="private",
        description_full=description,
        first_seen=NOW,
    )
    photo_conflict = listing(
        "photo-conflict",
        neighborhood="Младост",
        price_eur=240000,
        image_phash="aaaaaaaaaaaaaaab",
    )
    description_conflict = listing(
        "description-conflict",
        neighborhood="Център",
        description_full=description,
    )
    deleted = listing(
        "deleted-old",
        contact_phone="+359888000000",
        seller_type="private",
        neighborhood="Лозенец",
        image_count=4,
        is_active=False,
        first_seen=NOW - timedelta(days=30),
    )
    phone_rows = [
        listing(
            f"phone-{index}",
            contact_phone="+359888000000",
            seller_type="agency",
            neighborhood=f"Район {index}",
        )
        for index in range(14)
    ]
    db.add_all([suspicious, photo_conflict, description_conflict, deleted, *phone_rows])
    db.flush()
    db.add(
        Alert(
            listing_id=suspicious.id,
            alert_type="underpriced",
            zscore=-3.5,
            savings_pct=50,
        )
    )
    db.commit()

    summary = score_authenticity(db)
    db.refresh(suspicious)
    flags = json.loads(suspicious.authenticity_flags)

    assert summary["scored"] == 17
    assert suspicious.authenticity_score == 0
    assert {flag["signal"] for flag in flags} == {
        "photo_reuse",
        "price_plausibility",
        "seller_footprint",
        "description_reuse",
        "listing_hygiene",
    }
    assert any(flag["conflicts"] for flag in flags if flag["signal"] != "price_plausibility")
    assert lowest_scorers(db, limit=1)[0]["id"] == suspicious.id
