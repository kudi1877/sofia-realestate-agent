from src import main as main_module
from src.observability import RunRecorder


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
    def __init__(self, active_counts=None):
        self.upserted = []
        self.marked_inactive = []
        self.active_counts = active_counts or {}

    def upsert(self, listing_data, commit=True):
        row = dict(listing_data)
        row["_commit"] = commit
        self.upserted.append(row)

    def mark_inactive(self, source, active_ids):
        self.marked_inactive.append((source, active_ids))
        return 0

    def count_active_by_source(self, source):
        return self.active_counts.get(source, 0)

    def mark_stale_inactive_as_sold(self, days):
        return 0

    def count_off_market(self):
        return 0


class FakeScraper:
    def __init__(self, rows):
        self.rows = rows

    def scrape(self):
        return [dict(row) for row in self.rows]


class FakeDb:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_cmd_scrape_upserts_unique_winners_and_flagged_duplicates(monkeypatch):
    repo = FakeRepo(active_counts={"imotbg": 1, "imotiinfo": 1})
    db = FakeDb()

    monkeypatch.setattr(main_module, "get_db", lambda: db)
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
    assert {row["_commit"] for row in repo.upserted} == {False}
    assert db.commits == 1


def test_cmd_scrape_skips_mark_inactive_for_partial_source(monkeypatch):
    repo = FakeRepo(active_counts={"imotbg": 10})
    recorder = RunRecorder()
    db = FakeDb()

    monkeypatch.setattr(main_module, "MARK_INACTIVE_MIN_RATIO", 0.5)
    monkeypatch.setattr(main_module, "get_db", lambda: db)
    monkeypatch.setattr(main_module, "ListingRepository", lambda db: repo)
    monkeypatch.setattr(main_module, "update_neighborhood_stats", lambda db: None)
    monkeypatch.setattr(
        main_module,
        "ImotBgScraper",
        lambda: FakeScraper([listing("imotbg", f"imotbg-{index}") for index in range(4)]),
    )
    monkeypatch.setattr(main_module, "HomesBgScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "ImotiInfoScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "ImotiNetScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "PropertyBGScraper", lambda: FakeScraper([]))

    main_module.cmd_scrape(recorder=recorder)

    assert repo.marked_inactive == []
    assert recorder.status == "partial"
    assert recorder.errors == [
        "Skipping mark_inactive for imotbg: scraped 4 listings vs 10 active in DB (40.0% < 50%)"
    ]


def test_cmd_scrape_marks_inactive_when_source_count_is_normal(monkeypatch):
    repo = FakeRepo(active_counts={"imotbg": 10})
    db = FakeDb()

    monkeypatch.setattr(main_module, "MARK_INACTIVE_MIN_RATIO", 0.5)
    monkeypatch.setattr(main_module, "get_db", lambda: db)
    monkeypatch.setattr(main_module, "ListingRepository", lambda db: repo)
    monkeypatch.setattr(main_module, "update_neighborhood_stats", lambda db: None)
    monkeypatch.setattr(
        main_module,
        "ImotBgScraper",
        lambda: FakeScraper([listing("imotbg", f"imotbg-{index}") for index in range(6)]),
    )
    monkeypatch.setattr(main_module, "HomesBgScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "ImotiInfoScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "ImotiNetScraper", lambda: FakeScraper([]))
    monkeypatch.setattr(main_module, "PropertyBGScraper", lambda: FakeScraper([]))

    main_module.cmd_scrape()

    assert repo.marked_inactive == [("imotbg", [f"imotbg-{index}" for index in range(6)])]
