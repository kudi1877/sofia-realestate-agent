from bs4 import BeautifulSoup

from src.scrapers.homesbg import HomesBgScraper
from src.scrapers.imotbg import ImotBgScraper
from src.scrapers.imotiinfo import ImotiInfoScraper
from src.scrapers.imotinet import ImotiNetScraper
from src.scrapers.propertybg import PropertyBGScraper


def test_imotbg_extracts_protocol_relative_card_image():
    card = BeautifulSoup(
        """
        <div class="item TOP">
          <a class="saveSlink" href="//www.imot.bg/obiava-1abc">град София, Център</a>
          <img class="pic" src="//imotstatic.example.test/photo.jpg">
          <div class="price">100 000 €</div>
          2-СТАЕН град София, Център 100 000 € 50 кв. м
        </div>
        """,
        "html.parser",
    ).div

    parsed = ImotBgScraper()._parse_listing_item(card)

    assert parsed["image_url"] == "https://imotstatic.example.test/photo.jpg"


def test_imotiinfo_extracts_first_picture_from_embedded_json():
    parsed = ImotiInfoScraper()._parse_listing(
        {
            "id": "info-1",
            "url": "/obiava/123/prodava-2-staen-grad-sofiya-centar",
            "price": "100000",
            "currency": "EUR",
            "nraionsMob": "Център, град София",
            "summary": "50 кв.м",
            "pubtypetxt": "2-стаен",
            "pictures": [{"src": "//imotstatic.example.test/info.jpg"}],
        }
    )

    assert parsed["image_url"] == "https://imotstatic.example.test/info.jpg"


def test_imotinet_extracts_relative_card_image():
    card = BeautifulSoup(
        """
        <li class="clearfix">
          <a href="/bg/obiava/prodava/sofia/centar/apartament/12345/">2-стаен 50 m2</a>
          <img src="/web/files/obiavi/thumb.jpg">
          <span class="price">€ 100,000</span>
        </li>
        """,
        "html.parser",
    ).li

    parsed = ImotiNetScraper()._parse_listing(card)

    assert parsed["image_url"] == "https://www.imoti.net/web/files/obiavi/thumb.jpg"


def test_propertybg_extracts_lazy_loaded_card_image():
    card = BeautifulSoup(
        """
        <div class="panel offer">
          <a href="/property-12345-apartment-for-sale-in-sofia.html">2 bedroom apartment</a>
          <div class="item prop_image_url b-lazy" data-blazy="https://static.example.test/property.jpg"></div>
          <span class="price">€ 100,000</span>
          Sofia / Center district Area: 50 sq.m
        </div>
        """,
        "html.parser",
    ).div

    parsed = PropertyBGScraper()._parse_listing_from_element(card)

    assert parsed["image_url"] == "https://static.example.test/property.jpg"


def test_homesbg_builds_thumbnail_from_api_photo_metadata():
    item = {
        "photo": {"path": "2026-07-11_2/", "name": "120032156"},
    }

    assert HomesBgScraper._primary_image_url(item) == (
        "https://g1.homes.bg/2026-07-11_2/120032156b.jpg"
    )


def test_homesbg_falls_back_to_first_photo():
    item = {
        "photos": [{"path": "/2026-07-11_2/", "name": "120032157"}],
    }

    assert HomesBgScraper._primary_image_url(item) == (
        "https://g1.homes.bg/2026-07-11_2/120032157b.jpg"
    )
