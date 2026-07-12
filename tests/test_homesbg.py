from src.scrapers.homesbg import HomesBgScraper


def api_item(source_id: str, location: str) -> dict:
    return {
        "id": source_id,
        "type": "as",
        "viewHref": f"/offer/{source_id}",
        "location": location,
        "title": "Двустаен, 60m²",
        "description": "Тухла",
        "price": {
            "value": "120,000",
            "currency": "EUR",
            "price_per_square_meter": "2,000 EUR/m²",
        },
    }


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


def test_request_params_match_homes_infinite_scroll_contract():
    assert HomesBgScraper._request_params(2) == {
        "typeId": "ApartmentSell",
        "locationId": "1",
        "startIndex": 20,
        "stopIndex": 39,
    }
    assert HomesBgScraper._request_params(1, "rent") == {
        "typeId": "ApartmentRent",
        "locationId": "1",
        "startIndex": 0,
        "stopIndex": 19,
    }


def test_rental_response_uses_separate_source_kind_and_type_code():
    item = api_item("rent-1", "жк. Лозенец, София")
    item["type"] = "ar"
    item["price"]["value"] = "750"

    listing = HomesBgScraper(deal_type="rent")._parse_listing(item)

    assert listing["source"] == "homesbg-rent"
    assert listing["listing_kind"] == "rent"
    assert listing["property_type"] == "apartment"


def test_scrape_continues_when_api_page_has_no_parsed_sofia_matches(monkeypatch):
    payloads = [
        {
            "result": [api_item("outside", "Пловдив")],
            "hasMoreItems": True,
            "offersCount": 100,
        },
        {
            "result": [api_item("sofia", "жк. Лозенец, София")],
            "hasMoreItems": False,
            "offersCount": 100,
        },
    ]
    requested_params = []

    def fake_get(url, *, params, **kwargs):
        requested_params.append(params)
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr("src.scrapers.homesbg.httpx.get", fake_get)
    monkeypatch.setattr("src.scrapers.homesbg.time.sleep", lambda _seconds: None)

    listings = HomesBgScraper(max_pages=2).scrape()

    assert [listing["source_id"] for listing in listings] == ["sofia"]
    assert requested_params == [
        HomesBgScraper._request_params(1),
        HomesBgScraper._request_params(2),
    ]


def test_api_exhaustion_uses_raw_results_and_metadata():
    assert HomesBgScraper._api_is_exhausted(
        {"hasMoreItems": True, "offersCount": 100},
        raw_result_count=20,
        stop_index=19,
    ) is False
    assert HomesBgScraper._api_is_exhausted(
        {"hasMoreItems": False, "offersCount": 100},
        raw_result_count=20,
        stop_index=39,
    ) is True
