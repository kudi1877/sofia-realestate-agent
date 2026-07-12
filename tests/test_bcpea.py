from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing
from src.database.repository import ListingRepository
from src.scrapers.bcpea import BCPEAScraper
from src.utils.time import utc_now


def card_html(*, price="195 583.00 лв.", end="31.12.2099") -> str:
    return f"""
    <div class="item__group">
      <div class="title">Двустаен апартамент</div>
      <div class="category">65.00 кв.м</div>
      <div class="content--price"><div class="price">{price}</div></div>
      <div class="label__group"><div class="label">Адрес</div><div class="info">гр. София, ж.к. Люлин 5</div></div>
      <div class="label__group"><div class="label">ЧАСТЕН СЪДЕБЕН ИЗПЪЛНИТЕЛ</div><div class="info">Тестов ЧСИ</div></div>
      <div class="label__group"><div class="label">СРОК</div><div class="info">от 01.12.2099 до {end}</div></div>
      <a href="/properties/90123"><img src="/upload/90123/photo.jpg" /></a>
    </div>
    """


def test_parse_server_rendered_card_converts_bgn_and_emits_auction_fields():
    card = BeautifulSoup(card_html(), "html.parser").div

    row = BCPEAScraper.parse_card(card)

    assert row["source"] == "bcpea"
    assert row["source_id"] == "90123"
    assert row["listing_kind"] == "auction"
    assert row["price_eur"] == 100001.53
    assert row["price_bgn"] == 195583.0
    assert row["price_per_sqm_eur"] == 1538.49
    assert row["neighborhood"] == "Люлин 5"
    assert row["rooms"] == 2
    assert row["bailiff_name"] == "Тестов ЧСИ"


def test_expired_auction_card_is_not_returned():
    card = BeautifulSoup(card_html(end="01.01.2020"), "html.parser").div

    assert BCPEAScraper.parse_card(card) is None


def test_detail_parser_best_effort_extracts_quarter_and_case_number():
    soup = BeautifulSoup(
        """
        <div class="label__group"><div class="label">Квартал</div><div class="info">ж.к. Манастирски ливади</div></div>
        <div class="label__group"><div class="label">Адрес</div><div class="info">гр. София, ул. Мур 41</div></div>
        <div class="label__group"><div class="label">ОПИСАНИЕ</div><div class="info">по изпълнително дело № 20267880400123</div></div>
        """,
        "html.parser",
    )

    details = BCPEAScraper.parse_detail(soup)

    assert details["neighborhood"] == "Манастирски ливади"
    assert details["case_number"] == "20267880400123"


def test_repository_expires_only_ended_auctions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now()

    def row(source_id, kind, end):
        return Listing(
            source="test",
            source_id=source_id,
            listing_kind=kind,
            url=f"https://example.test/{source_id}",
            neighborhood="Люлин",
            property_type="apartment",
            area_sqm=50,
            price_eur=100000,
            price_per_sqm_eur=2000,
            auction_end=end,
            is_active=True,
        )

    expired = row("expired", "auction", now - timedelta(seconds=1))
    live = row("live", "auction", now + timedelta(days=1))
    sale = row("sale", "sale", now - timedelta(days=1))
    db.add_all([expired, live, sale])
    db.commit()

    assert ListingRepository(db).expire_auctions(now=now) == 1
    assert expired.is_active is False
    assert live.is_active is True
    assert sale.is_active is True
