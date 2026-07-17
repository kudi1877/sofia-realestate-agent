from src.utils.deduplication import (
    deduplicate_listings,
    generate_fingerprint,
    get_price_range,
    normalize_neighborhood,
)


def listing(**overrides):
    base = {
        "source": "imotbg",
        "source_id": "imotbg-1",
        "url": "https://example.test/listing",
        "neighborhood": "Люлин",
        "area_sqm": 65,
        "price_eur": 100000,
        "property_type": "apartment",
        "rooms": 2,
    }
    base.update(overrides)
    return base


def test_neighborhood_prefixes_normalize_to_same_value():
    assert normalize_neighborhood("жк Люлин") == "люлин"
    assert normalize_neighborhood("ж.к. Люлин") == "люлин"
    assert normalize_neighborhood("Люлин район") == "люлин"


def test_small_price_change_stays_in_same_price_band():
    assert get_price_range(100000) == get_price_range(102000)


def test_large_price_change_moves_to_different_price_band():
    assert get_price_range(100000) != get_price_range(112000)


def test_missing_rooms_for_studio_uses_one_room_fingerprint():
    studio_without_rooms = listing(rooms=None, property_type="studio")
    studio_with_room = listing(rooms=1, property_type="studio")

    assert generate_fingerprint(studio_without_rooms) == generate_fingerprint(studio_with_room)


def test_missing_area_uses_zero_area_bucket():
    without_area = listing(area_sqm=None)

    assert generate_fingerprint(without_area).split("_")[1] == "0"


def test_sale_and_rent_ads_never_share_a_fingerprint():
    sale = listing(listing_kind="sale")
    rental = listing(listing_kind="rent")

    assert generate_fingerprint(sale) != generate_fingerprint(rental)


def test_deduplicate_keeps_highest_priority_source_as_winner():
    low_priority = listing(source="propertybg", source_id="propertybg-1")
    high_priority = listing(source="imotiinfo", source_id="imotiinfo-1")

    result = deduplicate_listings([low_priority, high_priority])

    assert result.duplicates_removed == 1
    assert result.unique_listings == [high_priority]
    assert high_priority["is_duplicate"] is False
    assert low_priority["is_duplicate"] is True
    assert low_priority["duplicate_of"] == "imotiinfo-1"


def test_duplicate_counts_are_tracked_by_losing_source():
    winner = listing(source="imotiinfo", source_id="imotiinfo-1")
    duplicate = listing(source="homesbg", source_id="homesbg-1")

    result = deduplicate_listings([winner, duplicate])

    assert result.duplicates_by_source == {"homesbg": 1}
    assert result.canonical_ids["homesbg-1"] == result.canonical_ids["imotiinfo-1"]


def test_deduplication_result_exposes_flagged_duplicate_listings():
    winner = listing(source="imotiinfo", source_id="imotiinfo-1")
    duplicate = listing(source="imotbg", source_id="imotbg-1")

    result = deduplicate_listings([winner, duplicate])

    assert result.unique_listings == [winner]
    assert result.duplicate_listings == [duplicate]
    assert duplicate["is_duplicate"] is True
    assert duplicate["duplicate_of"] == "imotiinfo-1"


def test_canonical_listing_borrows_missing_attributes_from_twin():
    # TIN-520: an olx twin often carries year_built/floor the canonical lacks.
    winner = listing(source="homesbg", source_id="homesbg-1", floor=None, year_built=None)
    twin = listing(
        source="olx",
        source_id="olx-1",
        floor=1,
        total_floors=5,
        year_built=2026,
        construction_type="brick",
    )

    result = deduplicate_listings([winner, twin])

    assert result.unique_listings == [winner]
    assert winner["floor"] == 1
    assert winner["total_floors"] == 5
    assert winner["year_built"] == 2026
    assert winner["construction_type"] == "brick"


def test_backfill_never_overwrites_existing_canonical_values():
    winner = listing(source="homesbg", source_id="homesbg-1", floor=3, year_built=2010)
    twin = listing(source="olx", source_id="olx-1", floor=1, year_built=2026)

    deduplicate_listings([winner, twin])

    assert winner["floor"] == 3
    assert winner["year_built"] == 2010
