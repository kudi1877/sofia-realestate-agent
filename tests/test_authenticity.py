import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.analysis.authenticity as authenticity
from src.analysis.authenticity import (
    _near_hash_conflicts,
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


def test_comparison_pool_excludes_old_inactive_listings(monkeypatch):
    # 2026-07-13 regression: score_authenticity() pulled in the ENTIRE
    # historical corpus (30,433 rows vs 6,386 active), which hung the
    # nightly for 48+ hours. Listings inactive well beyond the lookback
    # window must not be scanned at all.
    monkeypatch.setattr(authenticity, "AUTHENTICITY_REPOST_LOOKBACK_DAYS", 90)
    db = session()
    active = listing("active-1", first_seen=NOW, last_seen=NOW)
    recent_inactive = listing(
        "recent-gone", is_active=False, last_seen=NOW - timedelta(days=10)
    )
    ancient_inactive = listing(
        "ancient-gone", is_active=False, last_seen=NOW - timedelta(days=400)
    )
    db.add_all([active, recent_inactive, ancient_inactive])
    db.commit()

    # score_authenticity() re-derives its own cutoff from utc_now(), so
    # exercise the query directly rather than freezing time end-to-end.
    from datetime import timedelta as _td
    from src.utils.time import utc_now as _utc_now
    from sqlalchemy import or_ as _or_

    cutoff = _utc_now() - _td(days=90)
    pool_ids = {
        row.source_id
        for row in db.query(Listing).filter(
            Listing.listing_kind == "sale",
            _or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
            _or_(Listing.is_active.is_(True), Listing.last_seen >= cutoff),
        )
    }
    assert pool_ids == {"active-1", "recent-gone"}
    assert "ancient-gone" not in pool_ids


def test_near_hash_conflicts_skips_pathological_buckets(monkeypatch):
    # Defensive cap: a bucket bigger than AUTHENTICITY_MAX_HASH_CANDIDATES
    # must be skipped, not fully cross-compared (the actual 2026-07-13
    # blowup mechanism when many listings share near-identical hashes).
    monkeypatch.setattr(authenticity, "AUTHENTICITY_MAX_HASH_CANDIDATES", 2)
    rows = [
        listing(f"dup-{index}", image_phash="0000000000000000")
        for index in range(5)
    ]

    conflicts = find_photo_reuse(rows)

    assert conflicts == {}


def test_near_hash_conflicts_still_matches_under_the_cap(monkeypatch):
    monkeypatch.setattr(authenticity, "AUTHENTICITY_MAX_HASH_CANDIDATES", 400)
    left = listing("left", image_phash="0123456789abcdef", price_eur=100000)
    right = listing(
        "right",
        canonical_id="different",
        neighborhood="Младост",
        price_eur=400000,
        image_phash="0123456789abcdee",
    )

    conflicts = find_photo_reuse([left, right])

    assert right in conflicts[id(left)]
