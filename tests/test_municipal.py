from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing, SeenNotice
from src.enrichment.llm_extract import ProviderResult
from src.scrapers import municipal
from src.scrapers.municipal import MunicipalNotice, MunicipalNoticeWatcher


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


SOFIA_INDEX = """
<main>
  <div class="asset-abstract">
    <h4>Столична община обявява публичен търг за продажба на апартамент в Люлин</h4>
    <a href="/w/7600001">Прочетете повече</a><h5>09.07.2026</h5>
  </div>
  <div class="asset-abstract">
    <h4>Конкурс за отдаване под наем на нежилищен имот</h4>
    <a href="/w/7600002">Прочетете повече</a><h5>08.07.2026</h5>
  </div>
  <div class="asset-abstract">
    <h4>Публичен търг с явно наддаване за продажба на поземлен имот</h4>
    <a href="https://www.sofia.bg/w/7600003?tracking=1">Прочетете повече</a><h5>07.07.2026</h5>
  </div>
</main>
"""

SOAPI_INDEX = """
<ul>
  <li><a href="/decisions-soapi/-/asset_publisher/key/content/2083060?x=1">Решение № 192 от 18 май 2026 г.</a></li>
  <li><a href="/decisions-soapi/-/asset_publisher/key/content/2083005">Решение № 191 от 18 май 2026 г.</a></li>
  <li><a href="/decisions-soapi/-/asset_publisher/key/content/2082947">Решение № 190 от 18 май 2026 г.</a></li>
</ul>
"""


def test_parse_official_indexes_returns_stable_notice_fields():
    sofia = MunicipalNoticeWatcher.parse_index(BeautifulSoup(SOFIA_INDEX, "html.parser"), "sofia_tenders")
    soapi = MunicipalNoticeWatcher.parse_index(BeautifulSoup(SOAPI_INDEX, "html.parser"), "soapi")

    assert [row.source_id for row in sofia] == ["7600001", "7600002", "7600003"]
    assert sofia[0].notice_date == datetime(2026, 7, 9, 23, 59, 59)
    assert sofia[2].url == "https://www.sofia.bg/w/7600003"
    assert len(soapi) == 3
    assert soapi[0].source_id == "2083060"
    assert soapi[0].notice_date == datetime(2026, 5, 18, 23, 59, 59)


class FakeProvider:
    name = "fake"

    def extract_notice(self, text):
        assert "апартамент" in text.casefold()
        return ProviderResult(
            data={
                "property_type": "apartment",
                "address": "гр. София, ж.к. Люлин 5, бл. 500",
                "area_sqm": 70,
                "starting_price": 100000,
                "currency": "EUR",
                "deadline": "2026-08-20",
            },
            model="fixture",
        )


def test_watcher_diffs_notices_and_creates_one_auction_listing(monkeypatch):
    db = session()
    watcher = MunicipalNoticeWatcher(delay_seconds=0)
    notices = [
        MunicipalNotice(
            source="sofia_tenders",
            source_id="sale-1",
            title="Публичен търг за продажба на апартамент в Люлин",
            notice_date=datetime(2026, 7, 10),
            url="https://www.sofia.bg/w/sale-1",
        ),
        MunicipalNotice(
            source="sofia_tenders",
            source_id="rent-1",
            title="Отдаване под наем на общински имот",
            notice_date=datetime(2026, 7, 10),
            url="https://www.sofia.bg/w/rent-1",
        ),
    ]
    monkeypatch.setattr(watcher, "fetch_notices", lambda: notices)
    monkeypatch.setattr(
        watcher,
        "fetch_notice_text",
        lambda _notice: (
            "Продажба на апартамент с площ 70 кв.м. Начална цена 100 000 EUR.",
            "https://www.sofia.bg/documents/sale-1.pdf",
        ),
    )

    first = watcher.run(db, provider=FakeProvider(), force=True, now=datetime(2026, 7, 12))
    second = watcher.run(db, provider=FakeProvider(), force=True, now=datetime(2026, 7, 12))

    assert first["new_notices"] == 2
    assert first["listings_created"] == 1
    assert second["new_notices"] == 0
    assert second["listings_created"] == 0
    assert db.query(SeenNotice).count() == 2
    row = db.query(Listing).one()
    assert row.source == "municipal"
    assert row.listing_kind == "auction"
    assert row.neighborhood == "Люлин 5"
    assert row.auction_end == datetime(2026, 8, 20, 23, 59, 59)
    assert row.price_eur == 100000
    assert row.price_per_sqm_eur == 1428.57
    assert db.query(SeenNotice).filter_by(source_id="sale-1").one().listing_id == row.id


def test_soapi_candidate_stays_pending_when_provider_is_off(monkeypatch):
    db = session()
    watcher = MunicipalNoticeWatcher(delay_seconds=0)
    notice = MunicipalNotice(
        source="soapi",
        source_id="192",
        title="Решение № 192 от 18 май 2026 г.",
        notice_date=datetime(2026, 5, 18),
        url="https://council.sofia.bg/decisions-soapi/192",
    )
    monkeypatch.setattr(watcher, "fetch_notices", lambda: [notice])
    monkeypatch.setattr(
        watcher,
        "fetch_notice_text",
        lambda _notice: ("Публичен търг за продажба на общински имот в София.", None),
    )

    summary = watcher.run(db, provider_name="off", force=True, now=datetime(2026, 7, 12))

    seen = db.query(SeenNotice).one()
    assert summary["pending"] == 1
    assert summary["listings_created"] == 0
    assert seen.processed_at is None
    assert seen.last_error == "provider_off"


def test_pdf_text_is_preferred_over_detail_html(monkeypatch):
    def handler(request):
        if request.url.path.endswith("notice.pdf"):
            return httpx.Response(200, content=b"%PDF-fixture", headers={"content-type": "application/pdf"})
        return httpx.Response(
            200,
            text='<main>HTML fallback <a href="/notice.pdf">PDF</a></main>',
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(municipal, "pdf_extract_text", lambda _stream: "Текст от PDF")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    watcher = MunicipalNoticeWatcher(delay_seconds=0, client=client)
    notice = SeenNotice(
        source="sofia_tenders",
        source_id="pdf-1",
        title="Продажба на апартамент",
        url="https://www.sofia.bg/w/pdf-1",
        is_residential=True,
    )

    text, pdf_url = watcher.fetch_notice_text(notice)

    assert text == "Текст от PDF"
    assert pdf_url == "https://www.sofia.bg/notice.pdf"


def test_detail_uses_longest_liferay_article():
    def handler(_request):
        return httpx.Response(
            200,
            text="""
                <div class="journal-content-article">Shared agency header</div>
                <div class="asset-full-content">
                  Решение № 192: публичен търг за продажба на поземлен имот.
                  Начална тръжна цена 255 000 EUR.
                </div>
            """,
        )

    watcher = MunicipalNoticeWatcher(
        delay_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    notice = SeenNotice(
        source="soapi",
        source_id="192",
        title="Решение № 192",
        url="https://council.sofia.bg/decisions-soapi/192",
        is_residential=True,
    )

    text, _pdf_url = watcher.fetch_notice_text(notice)

    assert "255 000 EUR" in text
    assert "Shared agency header" not in text


def test_weekday_gate_makes_no_network_request(monkeypatch):
    db = session()
    watcher = MunicipalNoticeWatcher(delay_seconds=0)
    monkeypatch.setattr(watcher, "fetch_notices", lambda: (_ for _ in ()).throw(AssertionError("network")))

    summary = watcher.run(db, provider_name="off", now=datetime(2026, 7, 12))

    assert summary["skipped"] == "weekday_gate"
    assert db.query(SeenNotice).count() == 0
