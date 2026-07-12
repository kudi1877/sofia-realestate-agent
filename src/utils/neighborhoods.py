"""Canonical neighborhood names (TIN-468).

Listings arrive with three flavors of the same place:
  - Cyrillic with prefixes:  "гр. Банкя", "с. Лозен", "ж.к. Люлин 5"
  - Bare Cyrillic:           "Банкя", "Люлин 5"
  - Latin slugs (imoti.net builds names from transliterated URL slugs):
                             "Bankja", "Malinova Dolina", "Lulin 5"

Before canonicalization these formed separate stat groups — market.json had
BOTH "Bankja" (€141/m², plot-dominated) and "гр. Банкя" (€1,878/m²), and the
sidebar listed "Malinova Dolina" and "Малинова долина" as different hoods.

`canonicalize_neighborhood` is the single normalization choke point, applied
to every scraped listing in cmd_scrape and used by the one-time DB repair.
It is deterministic: same input → same output, so repaired history and
future scrapes always group together.
"""

from __future__ import annotations

import re

from src.config import SOFIA_NEIGHBORHOODS

# Leading settlement/complex prefixes to strip (they denote the same place).
_PREFIX_RE = re.compile(r"^(?:гр|с|ж\.?к|кв|м-т)\.?\s+", re.IGNORECASE)

# imoti.net slug quirks that pure transliteration cannot recover.
_SPECIAL_LATIN = {
    "sofia center": "Център",
    "center": "Център",  # after the city prefix is stripped
    "lulin": "Люлин",  # slug uses 'u' where the name has 'ю'
}

# City prefix sometimes embedded in slugs: "Sofija Studentski Grad".
_CITY_PREFIX_RE = re.compile(r"^sofi[jy]?a\s+", re.IGNORECASE)

# Ad-card text that is definitely not a place name.
_JUNK_RE = re.compile(r"снимки|продава|обява|€|лв\.|кв\.?\s*м", re.IGNORECASE)

# Reverse transliteration, imoti.net's scheme. Digraphs/trigraphs first.
_TRANSLIT = [
    ("sht", "щ"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"),
    ("ja", "я"), ("ju", "ю"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"),
    # imoti.net slugs use 'j' for ж ("Drujba" → Дружба, "Hadji" → Хаджи);
    # я/ю are covered by the ja/ju digraphs above.
    ("z", "з"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"),
    ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("f", "ф"), ("h", "х"), ("c", "ц"), ("y", "ъ"), ("w", "в"),
    ("x", "кс"), ("q", "к"),
]

# 'ts' → ц ("Gotse" → Гоце) — but NOT inside the '-tski' adjective suffix
# ("Studentski" → Студентски, not Студенцки).
_TS_OK_RE = re.compile(r"ts(?!k)")

# Canonical-casing lookup from the known neighborhood list (casefolded key).
_KNOWN = {name.casefold(): name for name in SOFIA_NEIGHBORHOODS}

_CYRILLIC_RE = re.compile(r"[а-яА-Я]")


def _translit_word(word: str) -> str:
    lower = _TS_OK_RE.sub("ц", word.lower())
    out = []
    i = 0
    while i < len(lower):
        for src, dst in _TRANSLIT:
            if lower.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(lower[i])
            i += 1
    result = "".join(out)
    return result.capitalize() if word[:1].isupper() else result


def _default_casing(name: str) -> str:
    """Bulgarian convention: capitalize the first word, lowercase the rest.

    Proper-noun names ("Гоце Делчев") get their casing restored via the
    _KNOWN lookup instead; this is only the fallback for unknown names, and
    the DB repair pushes every historical row through the same rule so
    grouping stays consistent even where the aesthetic is off.
    """
    words = name.split()
    if not words:
        return name
    fixed = [words[0][:1].upper() + words[0][1:].lower()]
    fixed += [w if w.isdigit() else w.lower() for w in words[1:]]
    return " ".join(fixed)


def canonicalize_neighborhood(name: str | None) -> str:
    """Normalize a scraped neighborhood name to its canonical form."""
    if not name:
        return "Unknown"

    cleaned = _PREFIX_RE.sub("", name.strip()).strip().rstrip(".,;: ")
    if not cleaned:
        return "Unknown"

    # Junk guard (TIN-472): some parse fallbacks stored entire ad-card text
    # as the "neighborhood" ("Снимки 8 продава Парцел, 4500 м 2 София, …").
    # Real Sofia neighborhood names are short and never contain ad verbs.
    if len(cleaned) > 40 or _JUNK_RE.search(cleaned):
        return "Unknown"

    if not _CYRILLIC_RE.search(cleaned):
        # Latin slug → Cyrillic. Drop an embedded city prefix first
        # ("Sofija Studentski Grad" → "Studentski Grad").
        cleaned = _CITY_PREFIX_RE.sub("", cleaned).strip() or cleaned
        lower = cleaned.lower()
        for latin, cyr in _SPECIAL_LATIN.items():
            if lower == latin:
                return cyr
            if lower.startswith(latin + " "):
                cleaned = cyr + cleaned[len(latin):]
                break
        else:
            cleaned = " ".join(_translit_word(w) for w in cleaned.split())

    known = _KNOWN.get(cleaned.casefold())
    if known:
        return known
    return _default_casing(cleaned)
