from datetime import timedelta

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Alert, Base, Listing
from src.enrichment.availability import classify_response, ping_availability, select_candidates
from src.observability import RunRecorder
from src.utils.time import utc_now


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, source: str = "imotbg", **overrides) -> Listing:
    now = utc_now()
    values = {
        "source": source,
        "source_id": source_id,
        "url": f"https://example.test/obiava-{source_id}",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "area_sqm": 60,
        "price_eur": 120000,
        "price_per_sqm_eur": 2000,
        "first_seen": now,
        "last_seen": now - timedelta(hours=2),
        "is_active": True,
        "is_duplicate": False,
    }
    values.update(overrides)
    return Listing(**values)


def response(url: str, status: int = 200, text: str = "active listing") -> httpx.Response:
    return httpx.Response(status, text=text, request=httpx.Request("GET", url))


def test_source_classifiers_match_observed_dead_ad_shapes():
    cases = [
        (
            "imotbg",
            "https://www.imot.bg/obiava-123-prodava",
            response("https://www.imot.bg/obiavi/prodazhbi/dvustaen"),
        ),
        (
            "imotiinfo",
            "https://imoti.info/obiava/123-prodava",
            response("https://imoti.info/prodazhbi/grad-sofiya/dvustaini"),
        ),
        (
            "imotinet",
            "https://www.imoti.net/obiava/123",
            response("https://www.imoti.net/obiava/123", status=404),
        ),
        (
            "homesbg",
            "https://www.homes.bg/offer/example/as123",
            response(
                "https://www.homes.bg/offer/example/as123",
                status=404,
                text='{"type":"InactivePageError"}',
            ),
        ),
        (
            "propertybg",
            "https://www.property.bg/property-123-example/",
            response(
                "https://www.property.bg/property-123-example/",
                text='<div class="band r">Outdated listing</div><span class="band"> SOLD </span>',
            ),
        ),
    ]

    for source, requested_url, result in cases:
        assert classify_response(source, requested_url, result) == "gone"

    assert classify_response(
        "imotbg",
        "https://www.imot.bg/obiava-123-prodava",
        response("https://www.imot.bg/obiava-123-prodava"),
    ) == "live"
    assert classify_response(
        "imotbg",
        "https://www.imot.bg/obiava-123-prodava",
        response("https://www.imot.bg/obiava-123-prodava", status=503),
    ) == "unknown"


def test_candidate_selection_includes_recent_and_old_deals_and_round_robins():
    db = session()
    old = utc_now() - timedelta(days=30)
    recent_imot = listing("recent-imot")
    recent_homes = listing("recent-homes", source="homesbg")
    old_deal = listing("old-deal", first_seen=old)
    old_plain = listing("old-plain", first_seen=old)
    duplicate = listing("duplicate", is_duplicate=True)
    db.add_all([recent_imot, recent_homes, old_deal, old_plain, duplicate])
    db.flush()
    db.add(Alert(listing_id=old_deal.id, alert_type="underpriced", zscore=-2.0))
    db.commit()

    selected = select_candidates(db, max_per_run=3, recent_days=14)

    assert {row.source_id for row in selected} == {"recent-imot", "recent-homes", "old-deal"}
    assert selected[0].source != selected[1].source


class FakeClient:
    def get(self, url, **kwargs):
        if "gone" in url:
            return response("https://www.imot.bg/obiavi/prodazhbi/dvustaen")
        return response(url)


def test_pinger_updates_confirmation_and_active_state_but_never_last_seen():
    db = session()
    live = listing("live", url="https://www.imot.bg/obiava-live")
    gone = listing("gone", url="https://www.imot.bg/obiava-gone")
    db.add_all([live, gone])
    db.commit()
    original_last_seen = {live.id: live.last_seen, gone.id: gone.last_seen}

    counts = ping_availability(
        db,
        max_per_run=10,
        delay_seconds=0,
        client=FakeClient(),
        sleep=lambda _: None,
    )

    db.refresh(live)
    db.refresh(gone)
    assert counts == {"pinged": 2, "live": 1, "gone": 1, "unknown": 0}
    assert live.is_active is True
    assert gone.is_active is False
    assert live.availability_checked_at is not None
    assert gone.availability_checked_at is not None
    assert live.last_seen == original_last_seen[live.id]
    assert gone.last_seen == original_last_seen[gone.id]


def test_run_recorder_serializes_availability_counts():
    recorder = RunRecorder()
    recorder.set_availability(pinged=8, live=5, gone=2, unknown=1)

    assert recorder.to_dict()["availability"] == {
        "pinged": 8,
        "live": 5,
        "gone": 2,
        "unknown": 1,
    }
