"""Regression tests for TIN-447 (non-fatal Telegram) and TIN-448 (last_seen churn)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing
from src.database.repository import ListingRepository
from src.message_sender import _send_message


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def make_listing(source_id: str, *, is_active: bool, last_seen: datetime) -> Listing:
    return Listing(
        source="imotbg",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        neighborhood="Люлин",
        property_type="apartment",
        area_sqm=50,
        price_eur=100000,
        price_per_sqm_eur=2000,
        is_active=is_active,
        first_seen=last_seen,
        last_seen=last_seen,
    )


# ── TIN-447: Telegram must degrade gracefully, never raise ───────────────────


def test_send_message_returns_false_without_chat_id():
    # Empty chat_id used to raise ValueError, which crashed cmd_full before
    # the dashboard export step. It must return False instead.
    assert _send_message("hello", chat_id="") is False


# ── TIN-448: mark_inactive must not churn last_seen ──────────────────────────


def test_mark_inactive_does_not_touch_last_seen():
    db = session()
    repo = ListingRepository(db)

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    stale = make_listing("gone-1", is_active=True, last_seen=old)
    fresh = make_listing("seen-1", is_active=True, last_seen=old)
    db.add_all([stale, fresh])
    db.commit()

    # "seen-1" was re-scraped; "gone-1" wasn't → gets deactivated.
    marked = repo.mark_inactive("imotbg", ["seen-1"])
    assert marked == 1

    db.expire_all()
    stale = db.query(Listing).filter_by(source_id="gone-1").one()
    assert stale.is_active is False
    # The bulk UPDATE must not stamp last_seen (the old onupdate bug made
    # dead rows look "seen today" forever, so soft-deprecation never expired).
    assert stale.last_seen == old


def test_mark_inactive_skips_already_inactive_rows():
    db = session()
    repo = ListingRepository(db)

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    dead = make_listing("dead-1", is_active=False, last_seen=old)
    db.add(dead)
    db.commit()

    # Sweep with an unrelated active set — the already-inactive row must not
    # be counted or modified, run after run.
    assert repo.mark_inactive("imotbg", ["something-else"]) == 0
    assert repo.mark_inactive("imotbg", ["something-else"]) == 0

    db.expire_all()
    dead = db.query(Listing).filter_by(source_id="dead-1").one()
    assert dead.last_seen == old
    assert dead.is_active is False
