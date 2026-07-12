from src.main import select_published_neighborhood_stats


def group_stats(mean: float, median: float, count: int) -> dict:
    return {
        "mean": mean,
        "median": median,
        "count": count,
    }


def test_published_stats_prefer_apartments_then_houses():
    stats = {
        ("Бояна", "apartment", "brick"): group_stats(3200, 3100, 6),
        ("Бояна", "apartment", "all"): group_stats(3000, 2950, 10),
        ("Бояна", "all", "all"): group_stats(2200, 2400, 16),
        # No apartment tier → house tier wins over the mixed blend.
        ("Панчарево", "house", "all"): group_stats(1800, 1750, 8),
        ("Панчарево", "all", "all"): group_stats(1700, 1650, 12),
    }

    selected = select_published_neighborhood_stats(stats)

    assert selected == {
        "Бояна": stats[("Бояна", "apartment", "all")],
        "Панчарево": stats[("Панчарево", "house", "all")],
    }


def test_plot_dominated_blend_is_never_published():
    # The imot.bg benchmark exposed Нови Искър publishing €173/m² (land
    # blended into the all-types tier). No apartment/house tier + a blend
    # close to the plot tier → publish nothing for that hood.
    stats = {
        ("Нови Искър", "plot", "all"): group_stats(150, 140, 30),
        ("Нови Искър", "all", "all"): group_stats(200, 173, 40),
    }
    assert select_published_neighborhood_stats(stats) == {}


def test_sub_floor_blend_without_plot_tier_is_suppressed():
    # Land-dominated hood where plots never reached MIN_LISTINGS_PER_GROUP:
    # no plot tier exists to compare against, but €177/m² is impossible for
    # residential Sofia — the absolute floor suppresses it.
    stats = {
        ("Суходол", "all", "all"): group_stats(210, 177, 7),
    }
    assert select_published_neighborhood_stats(stats) == {}


def test_healthy_mixed_tier_still_publishes():
    # A hood with no apartment/house group but a genuinely residential blend
    # (well above both the plot tier and the absolute floor) still publishes.
    stats = {
        ("Симеоново", "plot", "all"): group_stats(400, 380, 6),
        ("Симеоново", "all", "all"): group_stats(2100, 2050, 14),
    }
    selected = select_published_neighborhood_stats(stats)
    assert selected == {"Симеоново": stats[("Симеоново", "all", "all")]}
