import json
from datetime import datetime, timedelta

import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing, PriceHistory
from src.enrichment.detail_fetcher import enrich_listing_details, select_enrichment_candidates
from src.observability import RunRecorder
from src.scrapers.homesbg import HomesBgScraper
from src.scrapers.imotbg import ImotBgScraper
from src.scrapers.imotiinfo import ImotiInfoScraper
from src.scrapers.imotinet import ImotiNetScraper
from src.scrapers.propertybg import PropertyBGScraper
from src.utils.phone import normalize_bulgarian_phone


NOW = datetime(2026, 7, 12, 12, 0)


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, source: str = "homesbg", **overrides) -> Listing:
    values = {
        "source": source,
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "area_sqm": 50,
        "price_eur": 100000,
        "price_per_sqm_eur": 2000,
        "first_seen": NOW - timedelta(days=30),
        "last_seen": NOW - timedelta(hours=2),
        "is_active": True,
        "is_duplicate": False,
    }
    values.update(overrides)
    return Listing(**values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0888 123 456", "+359888123456"),
        ("+359 888 123 456", "+359888123456"),
        ("00359 888 123 456", "+359888123456"),
        ("123", None),
    ],
)
def test_phone_normalization(raw, expected):
    assert normalize_bulgarian_phone(raw) == expected


def test_per_source_detail_parsers_follow_observed_markup():
    homes_state = {
        "data": {
            "offer": {
                "address": {"city": "Център, София", "coordinates": [42.7, 23.3]},
                "attributes": [
                    {"key": "notes", "value": "Full homes description"},
                    {"key": "floor", "value": "1-ви"},
                    {"key": "total_floors", "value": "5"},
                    {"key": "build_type", "value": "Тухла"},
                    {"key": "heating", "value": "Електричество"},
                    {"key": "furniture", "value": "Необзаведен"},
                ],
                "extras": [{"name": "Асансьор"}, {"name": "Паркомясто"}],
                "contacts": {"broker": {"name": "Broker", "phone": "0888123456"}, "agency": {"name": "Agency"}},
                "photos": [{"path": "2026/", "name": "photo"}],
            }
        }
    }
    homes = HomesBgScraper.parse_detail(
        BeautifulSoup(
            f"<script>window.__PRELOADED_STATE__ = {json.dumps(homes_state)};</script>",
            "html.parser",
        )
    )
    assert homes["description_full"] == "Full homes description"
    assert homes["latitude"] == 42.7
    assert homes["image_urls"] == ["https://g1.homes.bg/2026/photoo.jpg"]
    # TIN-520: structured attributes must survive the parse.
    assert homes["floor"] == 1
    assert homes["total_floors"] == 5
    assert homes["construction_type"] == "brick"
    assert homes["heating"] == "electric"
    assert homes["furnishing"] == "unfurnished"
    assert homes["has_elevator"] is True
    assert homes["parking"] == "parking_space"

    imot = ImotBgScraper.parse_detail(
        BeautifulSoup(
            """
            <div class="moreInfo"><span class="blockTitle"><h2>Описание на имота:</h2></span><div class="text">Imot full text</div></div>
            <h2>Местоположение: град София, Люлин</h2>
            <div class="smallPicturesGallery"><img src="//images.test/imot.jpg"></div>
            <div class="contactsBox"><div class="phone">0888 123 456</div></div>
            <script type="application/ld+json">{"@type":"Offer","seller":{"name":"Imot Agency"}}</script>
            """,
            "html.parser",
        )
    )
    assert imot["description_full"] == "Imot full text"
    assert imot["seller_name"] == "Imot Agency"

    info = ImotiInfoScraper.parse_detail(
        BeautifulSoup(
            """
            <div class="obiava"><div class="location">град София, Люлин</div><div class="description"><div class="text">Info full text</div></div></div>
            <div class="photos"><img src="//images.test/info.jpg"></div><div class="broker">Info Agency</div>
            <a href="tel:0888123456">call</a><script>{"mraion":[42.71,23.31,0]}</script>
            """,
            "html.parser",
        )
    )
    assert info["longitude"] == 23.31
    assert info["address"] == "град София, Люлин"

    net = ImotiNetScraper.parse_detail(
        BeautifulSoup(
            """
            <section id="js-ad-container" class="real-estate-offer"><span class="location">София, Люлин</span><div class="text">Net full text</div></section>
            <h6 class="contact-agency-name">Net Agency</h6><a class="hidden-phone">0888123456</a>
            <div class="gallery-slider-pics"><img src="/web/files/obiavi/1/images/photo.jpg"></div>
            """,
            "html.parser",
        )
    )
    assert net["description_full"] == "Net full text"
    assert net["image_urls"] == ["https://www.imoti.net/web/files/obiavi/1/images/photo.jpg"]

    prop = PropertyBGScraper.parse_detail(
        BeautifulSoup(
            """
            <script type="application/ld+json">{"@type":"Offer","description":"Property full text","image":"https://images.test/property.jpg"}</script>
            <span>Location</span><b class="font-large">district Lyulin / Sofia</b>
            <iframe src="https://www.google.com/maps?q=42.72,23.32&z=17"></iframe>
            <div class="avatar"></div><b class="font-large">Property Agent</b>
            <div id="prop_gallery_grid"><a href="https://images.test/property-2.jpg"></a></div>
            """,
            "html.parser",
        )
    )
    assert prop["description_full"] == "Property full text"
    assert prop["seller_name"] == "Property Agent"
    assert prop["latitude"] == 42.72


def test_candidate_selection_includes_oldest_backfill_and_post_enrichment_price_change():
    db = session()
    oldest = listing("oldest", first_seen=NOW - timedelta(days=100))
    changed = listing("changed", enriched_at=NOW - timedelta(days=5))
    stable = listing("stable", enriched_at=NOW - timedelta(days=1))
    db.add_all([oldest, changed, stable])
    db.flush()
    db.add_all(
        [
            PriceHistory(listing_id=changed.id, price_eur=95000, price_per_sqm_eur=1900, recorded_at=NOW),
            PriceHistory(listing_id=stable.id, price_eur=100000, price_per_sqm_eur=2000, recorded_at=NOW - timedelta(days=2)),
        ]
    )
    db.commit()

    selected = select_enrichment_candidates(db, max_per_run=10)

    assert [row.source_id for row in selected] == ["oldest", "changed"]


class HomesClient:
    def get(self, url, **kwargs):
        state = {
            "data": {
                "offer": {
                    "address": {"city": "Люлин", "coordinates": []},
                    "attributes": [{"key": "notes", "value": "Fresh full description"}],
                    "contacts": {"broker": {"phone": "0888 123 456"}, "agency": {}},
                    "photos": [{"path": "2026/", "name": "one"}, {"path": "2026/", "name": "two"}],
                }
            }
        }
        html = f"<script>window.__PRELOADED_STATE__ = {json.dumps(state)};</script>"
        return httpx.Response(200, text=html, request=httpx.Request("GET", url))


def test_enrichment_persists_private_fields_without_touching_last_seen():
    db = session()
    row = listing("enrich")
    db.add(row)
    db.commit()
    original_last_seen = row.last_seen

    summary = enrich_listing_details(db, max_per_run=1, delay_seconds=0, client=HomesClient(), sleep=lambda _: None)

    db.refresh(row)
    assert summary["enriched"] == 1
    assert row.description_full == "Fresh full description"
    assert row.contact_phone == "+359888123456"
    assert row.image_count == 2
    assert len(json.loads(row.image_urls)) == 2
    assert row.enriched_at is not None
    assert row.last_seen == original_last_seen


class BlockedClient:
    def __init__(self):
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return httpx.Response(403, request=httpx.Request("GET", url))


def test_three_consecutive_blocks_pause_source_and_mark_run_partial():
    db = session()
    db.add_all([listing(str(index), source="imotbg") for index in range(4)])
    db.commit()
    client = BlockedClient()
    recorder = RunRecorder()

    summary = enrich_listing_details(
        db,
        max_per_run=4,
        delay_seconds=0,
        client=client,
        sleep=lambda _: None,
        recorder=recorder,
    )

    assert client.calls == 3
    assert summary["backed_off_sources"] == ["imotbg"]
    assert recorder.status == "partial"
    assert "after 3 consecutive HTTP 403" in recorder.errors[0]
