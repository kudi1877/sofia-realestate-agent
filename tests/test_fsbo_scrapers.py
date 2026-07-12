from bs4 import BeautifulSoup

from src.scrapers.alo import AloScraper
from src.scrapers.bazar import BazarScraper
from src.scrapers.olx import OlxScraper
from src.analysis.data_health import SOURCE_KEYS
from src.utils.deduplication import get_source_priority


def olx_offer(index: int, *, category: int = 524, business: bool = False):
    return {
        "id": 1000 + index,
        "url": f"https://www.olx.bg/d/ad/test-{index}.html",
        "title": f"{index + 1}-стаен апартамент",
        "description": "Продава се светъл апартамент",
        "business": business,
        "category": {"id": category},
        "location": {"district": {"name": f"Люлин {index + 1}"}},
        "photos": [{"link": "https://images.test/photo/image;s={width}x{height}"}],
        "params": [
            {"key": "price", "value": {"value": 100000 + index * 10000, "currency": "EUR"}},
            {"key": "space", "value": {"key": str(50 + index)}},
            {"key": "atype", "value": {"key": str(index + 1)}},
            {"key": "floor", "value": {"label": "2-ри"}},
            {"key": "floors", "value": {"label": "8"}},
            {"key": "ctype", "value": {"key": "tuhla"}},
            {"key": "cyear", "value": {"key": "2020"}},
        ],
    }


def test_olx_batch_parses_five_offers_and_private_business_accounts():
    scraper = OlxScraper(max_pages=1)

    rows = [scraper.parse_offer(olx_offer(index, business=index % 2 == 0)) for index in range(5)]

    assert all(rows)
    assert [row["rooms"] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["seller_type"] == "agency"
    assert rows[1]["seller_type"] == "private"
    assert rows[0]["image_url"].endswith("image;s=640x480")
    assert rows[0]["construction_type"] == "brick"

    plot = olx_offer(9, category=529)
    plot["title"] = "Парцел за къща в Бистрица"
    plot["location"]["district"]["name"] = "Център"
    assert scraper.parse_offer(plot)["neighborhood"] == "Бистрица"


def bazar_card(index: int):
    return BeautifulSoup(
        f"""
        <div class="listItemContainer">
          <a class="listItemLink" data-id="{5000 + index}" href="/obiava-{5000 + index}/test" title="Продава {index + 1}-СТАЕН, гр. София, Люлин {index + 1}">
            <img class="cover" data-src="//images.test/{index}.jpg" />
            <span class="location">гр. София, Люлин {index + 1}</span>
            <span class="price">{120000 + index * 10000} <span class="currency">€</span></span>
          </a>
        </div>
        """,
        "html.parser",
    ).select_one(".listItemContainer")


def bazar_detail(index: int):
    return BeautifulSoup(
        f"""
        <div class="row"><div class="span4">Квадратура</div><div class="span8">{60 + index} кв.м.</div></div>
        <div class="row"><div class="span4">Етаж</div><div class="span8">3</div></div>
        <div class="row"><div class="span4">Етажност</div><div class="span8">8</div></div>
        <div class="row"><div class="span4">Година на строителство</div><div class="span8">2018</div></div>
        <div class="row"><div class="span4">Вид строителство</div><div class="span8">Тухла</div></div>
        <script type="application/ld+json">{{"@type":"Product","description":"Тестово описание {index}"}}</script>
        """,
        "html.parser",
    )


def test_bazar_batch_parses_three_server_rendered_detail_pages():
    scraper = BazarScraper(max_pages=1)

    rows = [scraper.parse_card(bazar_card(index), bazar_detail(index)) for index in range(3)]

    assert all(rows)
    assert [row["rooms"] for row in rows] == [1, 2, 3]
    assert rows[0]["neighborhood"] == "Люлин 1"
    assert rows[0]["area_sqm"] == 60
    assert rows[0]["image_url"] == "https://images.test/0.jpg"
    assert rows[0]["description"] == "Тестово описание 0"


def alo_card(index: int):
    room_words = ["Едностаен", "Двустаен", "Тристаен"]
    return BeautifulSoup(
        f"""
        <div class="listtop-item" id="adrows_{8000 + index}">
          <div class="listtop-publisher"><img class="listtop-logo" src="agency.jpg" />Агенция</div>
          <a href="/{room_words[index].lower()}-apartment-{8000 + index}"><h3>{room_words[index]} апартамент</h3></a>
          <div class="listtop-item-address">Люлин {index + 1}, София</div>
          <img class="listtop-image-img" src="user_files/{index}.jpg" />
          <div class="ads-params-row"><div class="ads-param-title">Цена:</div><div class="ads-params-cell">{90000 + index * 10000} €</div></div>
          <div class="ads-params-row"><div class="ads-param-title">Квадратура:</div><div class="ads-params-cell">{45 + index} кв.м</div></div>
          <div class="ads-params-row"><div class="ads-param-title">Етаж:</div><div class="ads-params-cell">2</div></div>
          <div class="ads-params-row"><div class="ads-param-title">Година на строителство:</div><div class="ads-params-cell">2021</div></div>
          <p class="listtop-desc">Тестово жилище</p>
        </div>
        """,
        "html.parser",
    ).select_one("[id^=adrows_]")


def test_alo_batch_parses_three_sofia_cards():
    scraper = AloScraper(max_pages=1)

    rows = [scraper.parse_card(alo_card(index)) for index in range(3)]

    assert all(rows)
    assert [row["rooms"] for row in rows] == [1, 2, 3]
    assert rows[0]["neighborhood"] == "Люлин 1"
    assert rows[0]["seller_type"] == "agency"
    assert rows[0]["image_url"] == "https://www.alo.bg/user_files/0.jpg"
    assert rows[0]["area_sqm"] == 45

    private_card = alo_card(1)
    private_card.select_one('[class*="publisher"]').decompose()
    private_card.select_one("h3").string = "Собственик продава двустаен апартамент"
    assert scraper.parse_card(private_card)["seller_type"] == "private"


def test_fsbo_sources_rank_below_specialist_portals():
    specialist_floor = get_source_priority("propertybg")

    assert specialist_floor < get_source_priority("olx")
    assert get_source_priority("olx") < get_source_priority("bazar") < get_source_priority("alo")
    assert {SOURCE_KEYS[name] for name in ("olx.bg", "bazar.bg", "alo.bg")} == {
        "olx",
        "bazar",
        "alo",
    }
