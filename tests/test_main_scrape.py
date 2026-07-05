from src import main as main_module


def listing(source, source_id):
    return {
        "source": source,
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "title": "Test listing",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "rooms": 2,
        "area_sqm": 65,
        "price_eur": 100000,
        "price_per_sqm_eur": 1538.46,
    }


class FakeRepo:
    def __init__(self):
        self.upserted = []
        self.marked_inactive = []

    def upsert(self, listing_data):
        self.upserted.append(dict(listing_data))

    def mark_inactive(self, source, active_ids):
        self.marked_inactive.append((source, active_ids))
        return 0


class FakeScraper:
    def __init__(self, rows):
        self.rows = rows

    def scrape(self):
        return [dict(row) for row in self.rows]


def test_cmd_scrape_upserts_unique_winners_and_flagged_duplicates(monkeypatch):
    repo = FakeRepo()

    monkeypatch.setattr(main_module, "get_db", lambda: object())
    monkeypatch.setattr(main_module, "ListingRepository", lambda db: repo)
    monkeypatch.setattr(main_module, "update_neighborhood_stats", lambda db: None)
    monkeypatch.setattr(
        main_module,
        "ImotBgScraper",
        lambda: FakeScraper([listing("imotbg", "imotbg-1")]),
    )
    monkeypatch.setattr(main_module, "HomesBgScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(
        main_module,
        "ImotiInfoScraper",
        lambda: FakeScraper([listing("imotiinfo", "imotiinfo-1")]),
    )
    monkeypatch.setattr(main_module, "ImotiNetScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "PropertyBGScraper", lambda: FakeScraper([]))

    saved_count = main_module.cmd_scrape()

    by_source_id = {row["source_id"]: row for row in repo.upserted}
    assert saved_count == 1
    assert set(by_source_id) == {"imotbg-1", "imotiinfo-1"}
    assert by_source_id["imotiinfo-1"]["is_duplicate"] is False
    assert by_source_id["imotbg-1"]["is_duplicate"] is True
    assert by_source_id["imotbg-1"]["duplicate_of"] == "imotiinfo-1"
