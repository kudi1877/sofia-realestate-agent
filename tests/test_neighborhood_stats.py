from src.main import select_published_neighborhood_stats


def group_stats(mean: float, median: float, count: int) -> dict:
    return {
        "mean": mean,
        "median": median,
        "count": count,
    }


def test_published_stats_prefer_apartments_and_fall_back_to_all_properties():
    stats = {
        ("Бояна", "apartment", "brick"): group_stats(3200, 3100, 6),
        ("Бояна", "apartment", "all"): group_stats(3000, 2950, 10),
        ("Бояна", "all", "all"): group_stats(2200, 2400, 16),
        ("Панчарево", "house", "all"): group_stats(1800, 1750, 8),
        ("Панчарево", "all", "all"): group_stats(1700, 1650, 12),
    }

    selected = select_published_neighborhood_stats(stats)

    assert selected == {
        "Бояна": stats[("Бояна", "apartment", "all")],
        "Панчарево": stats[("Панчарево", "all", "all")],
    }
