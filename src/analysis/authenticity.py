"""Authenticity scoring and bait-listing evidence collection."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from src.config import AUTHENTICITY_DEAL_MIN_SCORE
from src.database.models import Listing

PHOTO_HAMMING_THRESHOLD = 6
DESCRIPTION_HAMMING_THRESHOLD = 3
PRIVATE_SELLER_ACTIVE_AD_THRESHOLD = 15
MATERIAL_PRICE_DELTA_PCT = 25.0

PENALTIES = {
    "photo_reuse": 35,
    "price_plausibility": 30,
    "seller_footprint": 25,
    "description_reuse": 25,
    "few_photos": 10,
    "missing_floor_year": 10,
    "repost_pattern": 20,
}


def passes_authenticity_gate(listing: Listing) -> bool:
    return (
        listing.authenticity_score is None
        or listing.authenticity_score >= AUTHENTICITY_DEAL_MIN_SCORE
    )


def _identity(listing: Listing) -> str:
    return listing.canonical_id or f"{listing.source}:{listing.source_id}"


def _conflict_payload(listing: Listing) -> Dict[str, Any]:
    return {
        "id": listing.id,
        "source": listing.source,
        "url": listing.url,
        "neighborhood": listing.neighborhood,
        "price_eur": listing.price_eur,
    }


def _materially_different(left: Listing, right: Listing) -> bool:
    if left.neighborhood and right.neighborhood and left.neighborhood != right.neighborhood:
        return True
    if left.price_eur and right.price_eur:
        baseline = max(float(left.price_eur), float(right.price_eur))
        return abs(float(left.price_eur) - float(right.price_eur)) / baseline * 100 >= MATERIAL_PRICE_DELTA_PCT
    return False


def _near_hash_conflicts(
    rows: List[Listing],
    hash_for: Callable[[Listing], str | None],
    *,
    max_distance: int,
    require_material_difference: bool,
) -> Dict[int, List[Listing]]:
    """Find near 64-bit hashes via exact 8-bit bands, then verify Hamming distance."""
    valid = []
    buckets: Dict[tuple[int, int], List[int]] = defaultdict(list)
    for row_index, row in enumerate(rows):
        raw = hash_for(row)
        if not raw or not re.fullmatch(r"[0-9a-fA-F]{16}", raw):
            continue
        value = int(raw, 16)
        valid.append((row_index, value))
        for band in range(8):
            buckets[(band, (value >> (band * 8)) & 0xFF)].append(row_index)

    value_by_index = {row_index: value for row_index, value in valid}
    conflicts: Dict[int, List[Listing]] = defaultdict(list)
    checked = set()
    for row_index, value in valid:
        candidates = set()
        for band in range(8):
            candidates.update(buckets[(band, (value >> (band * 8)) & 0xFF)])
        for other_index in candidates:
            pair = tuple(sorted((row_index, other_index)))
            if row_index == other_index or pair in checked:
                continue
            checked.add(pair)
            left = rows[row_index]
            right = rows[other_index]
            if _identity(left) == _identity(right):
                continue
            if require_material_difference and not _materially_different(left, right):
                continue
            distance = (value ^ value_by_index[other_index]).bit_count()
            if distance > max_distance:
                continue
            conflicts[id(left)].append(right)
            conflicts[id(right)].append(left)
    return conflicts


def find_photo_reuse(rows: List[Listing]) -> Dict[int, List[Listing]]:
    """Return cross-canonical, materially inconsistent near-pHash matches."""
    return _near_hash_conflicts(
        rows,
        lambda row: row.image_phash,
        max_distance=PHOTO_HAMMING_THRESHOLD,
        require_material_difference=True,
    )


def shingled_description_hash(text: str | None) -> str | None:
    """Compute a 64-bit SimHash over normalized four-word shingles."""
    words = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    if len(words) < 12:
        return None
    shingles = {" ".join(words[index:index + 4]) for index in range(len(words) - 3)}
    weights = [0] * 64
    for shingle in shingles:
        value = int.from_bytes(
            hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(),
            "big",
        )
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)
    return f"{fingerprint:016x}"


def find_description_reuse(rows: List[Listing]) -> Dict[int, List[Listing]]:
    hashes = {
        id(row): shingled_description_hash(row.description_full or row.description)
        for row in rows
    }
    return _near_hash_conflicts(
        rows,
        lambda row: hashes[id(row)],
        max_distance=DESCRIPTION_HAMMING_THRESHOLD,
        require_material_difference=False,
    )


def _photo_count(listing: Listing) -> int:
    if listing.image_count is not None:
        return max(0, int(listing.image_count))
    if listing.image_urls:
        try:
            return len(json.loads(listing.image_urls))
        except (ValueError, TypeError):
            pass
    return 1 if listing.image_url else 0


def _repost_key(listing: Listing) -> tuple[Any, ...] | None:
    if not listing.contact_phone:
        return None
    return (
        listing.source,
        listing.contact_phone,
        listing.listing_kind,
        listing.neighborhood,
        listing.property_type,
        listing.rooms,
        round(float(listing.area_sqm or 0)),
    )


def _flag(
    signal: str,
    penalty_key: str,
    detail: str,
    conflicts: Iterable[Listing] = (),
) -> Dict[str, Any]:
    return {
        "signal": signal,
        "penalty": PENALTIES[penalty_key],
        "detail": detail,
        "conflicts": [_conflict_payload(row) for row in list(conflicts)[:3]],
    }


def score_authenticity(db: Session) -> Dict[str, Any]:
    """Recompute authenticity for active unique sale inventory."""
    all_rows = (
        db.query(Listing)
        .options(selectinload(Listing.alerts))
        .filter(
            Listing.listing_kind == "sale",
            or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
        )
        .all()
    )
    targets = [row for row in all_rows if row.is_active]
    photo_conflicts = find_photo_reuse(all_rows)
    description_conflicts = find_description_reuse(all_rows)

    active_by_phone: Dict[str, Dict[str, Listing]] = defaultdict(dict)
    inactive_by_repost_key: Dict[tuple[Any, ...], List[Listing]] = defaultdict(list)
    for row in all_rows:
        if row.is_active and row.contact_phone:
            active_by_phone[row.contact_phone][_identity(row)] = row
        repost_key = _repost_key(row)
        if repost_key and not row.is_active:
            inactive_by_repost_key[repost_key].append(row)

    distribution = {"red": 0, "amber": 0, "clear": 0}
    for row in targets:
        flags = []
        reused_photos = photo_conflicts.get(id(row), [])
        if reused_photos:
            flags.append(
                _flag(
                    "photo_reuse",
                    "photo_reuse",
                    f"Near-identical thumbnail appears on {len(reused_photos)} materially different ad(s)",
                    reused_photos,
                )
            )

        zscores = [
            float(alert.zscore)
            for alert in row.alerts
            if alert.alert_type == "underpriced" and alert.zscore is not None
        ]
        extreme_zscore = min(zscores) if zscores else None
        if (row.residual_pct is not None and row.residual_pct < -35) or (
            extreme_zscore is not None and extreme_zscore < -3
        ):
            detail = (
                f"Hedonic residual {row.residual_pct:.1f}%"
                if row.residual_pct is not None and row.residual_pct < -35
                else f"Price z-score {extreme_zscore:.2f}"
            )
            flags.append(_flag("price_plausibility", "price_plausibility", detail))

        phone_rows = active_by_phone.get(row.contact_phone or "", {})
        if row.seller_type == "private" and len(phone_rows) >= PRIVATE_SELLER_ACTIVE_AD_THRESHOLD:
            conflicts = [candidate for key, candidate in phone_rows.items() if key != _identity(row)]
            flags.append(
                _flag(
                    "seller_footprint",
                    "seller_footprint",
                    f"Private seller phone appears on {len(phone_rows)} active ads",
                    conflicts,
                )
            )

        reused_descriptions = description_conflicts.get(id(row), [])
        if reused_descriptions:
            flags.append(
                _flag(
                    "description_reuse",
                    "description_reuse",
                    f"Near-identical description appears on {len(reused_descriptions)} other ad(s)",
                    reused_descriptions,
                )
            )

        photo_count = _photo_count(row)
        if photo_count <= 1:
            flags.append(
                _flag(
                    "listing_hygiene",
                    "few_photos",
                    f"Only {photo_count} photo{'s' if photo_count != 1 else ''}",
                )
            )
        if row.floor is None and row.year_built is None:
            flags.append(
                _flag(
                    "listing_hygiene",
                    "missing_floor_year",
                    "Both floor and construction year are missing",
                )
            )

        reposts = [
            candidate
            for candidate in inactive_by_repost_key.get(_repost_key(row), [])
            if candidate.source_id != row.source_id
            and (
                not candidate.first_seen
                or not row.first_seen
                or candidate.first_seen < row.first_seen
            )
        ]
        if reposts:
            flags.append(
                _flag(
                    "listing_hygiene",
                    "repost_pattern",
                    "Same phone and property parameters reappeared under a fresh source ID",
                    reposts,
                )
            )

        row.authenticity_score = max(0, 100 - sum(int(flag["penalty"]) for flag in flags))
        row.authenticity_flags = json.dumps(flags, ensure_ascii=False)
        if row.authenticity_score < AUTHENTICITY_DEAL_MIN_SCORE:
            distribution["red"] += 1
        elif row.authenticity_score < 80:
            distribution["amber"] += 1
        else:
            distribution["clear"] += 1

    db.commit()
    return {"scored": len(targets), **distribution}


def lowest_scorers(db: Session, *, limit: int = 20) -> List[Dict[str, Any]]:
    rows = (
        db.query(Listing)
        .filter(
            Listing.is_active.is_(True),
            Listing.authenticity_score.isnot(None),
        )
        .order_by(Listing.authenticity_score.asc(), Listing.id)
        .limit(limit)
        .all()
    )
    result = []
    for row in rows:
        try:
            flags = json.loads(row.authenticity_flags or "[]")
        except (ValueError, TypeError):
            flags = []
        result.append(
            {
                "id": row.id,
                "score": row.authenticity_score,
                "source": row.source,
                "neighborhood": row.neighborhood,
                "price_eur": row.price_eur,
                "url": row.url,
                "reasons": [flag.get("detail") for flag in flags],
            }
        )
    return result
