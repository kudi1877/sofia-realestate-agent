"""imot.bg published average prices — external benchmark (TIN-476).

https://www.imot.bg/sredni-ceni publishes a table of average apartment
prices per Sofia neighborhood (windows-1251). We scrape the overall
€/m² column each run and attach it to market.json so the Market page can
show "our median vs imot.bg's average" — a running accuracy check.

Methodology caveat carried through to the UI: ours is a cross-portal
deduplicated MEDIAN; theirs is a single-portal AVERAGE. Divergence is
informative, not an error.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from src.utils.neighborhoods import canonicalize_neighborhood

BENCHMARK_URL = "https://www.imot.bg/sredni-ceni"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def parse_benchmark_table(html: str) -> Dict[str, float]:
    """Parse the sredni-ceni table → {canonical neighborhood: avg €/m²}.

    Row shape: [Район, 1-room price, €/m², 2-room price, €/m²,
                3-room price, €/m², overall €/m²] — we take the last column.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return {}

    result: Dict[str, float] = {}
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 8:
            continue
        name = cells[0]
        overall = cells[-1].replace(" ", "").replace("\xa0", "")
        if not re.fullmatch(r"\d{3,5}", overall):
            continue
        canonical = canonicalize_neighborhood(name)
        if canonical == "Unknown":
            continue
        # First occurrence wins if the table ever repeats a name.
        result.setdefault(canonical, float(overall))
    return result


def fetch_benchmark(timeout: float = 30.0) -> Optional[Dict[str, float]]:
    """Fetch + parse the live benchmark. Returns None on any failure —
    the export must degrade gracefully, never crash on this."""
    try:
        resp = httpx.get(
            BENCHMARK_URL,
            headers={"User-Agent": _UA},
            follow_redirects=True,
            timeout=timeout,
        )
        resp.raise_for_status()
        table = parse_benchmark_table(resp.content.decode("windows-1251", errors="replace"))
        if not table:
            logger.warning("imot.bg benchmark page parsed to 0 rows — layout change?")
            return None
        logger.info(f"imot.bg benchmark: {len(table)} neighborhoods parsed")
        return table
    except Exception as e:  # noqa: BLE001 — benchmark is strictly optional
        logger.warning(f"imot.bg benchmark fetch failed (continuing without): {e}")
        return None
