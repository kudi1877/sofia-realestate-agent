"""Bounded perceptual hashing of primary listing thumbnails."""

from __future__ import annotations

import json
import time
from io import BytesIO
from pathlib import Path
from typing import Callable, Dict, Protocol
from urllib.parse import urlparse

import httpx
import imagehash
from loguru import logger
from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.config import (
    HASH_DELAY_SECONDS,
    HASH_MAX_PER_RUN,
    IMAGE_HASH_CACHE_PATH,
    USER_AGENTS,
)
from src.database.models import Listing


class HttpClient(Protocol):
    def get(self, url: str, **kwargs) -> httpx.Response: ...

    def close(self) -> None: ...


def perceptual_hash(content: bytes) -> str:
    """Return a normalized 64-bit pHash for one decoded image."""
    with Image.open(BytesIO(content)) as image:
        return str(imagehash.phash(image.convert("RGB")))


def _load_cache(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {
        str(url): str(value)
        for url, value in data.items()
        if isinstance(url, str) and isinstance(value, str) and len(value) == 16
    }


def _write_cache(path: Path, cache: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(cache, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def hash_listing_images(
    db: Session,
    *,
    max_per_run: int = HASH_MAX_PER_RUN,
    delay_seconds: float = HASH_DELAY_SECONDS,
    cache_path: Path = IMAGE_HASH_CACHE_PATH,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, int]:
    """Hash unprocessed active thumbnails with a per-host request interval."""
    rows = (
        db.query(Listing)
        .filter(
            Listing.is_active.is_(True),
            or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
            Listing.image_url.isnot(None),
            Listing.image_phash.is_(None),
        )
        .order_by(Listing.first_seen.desc(), Listing.id)
        .limit(max_per_run)
        .all()
    )
    cache = _load_cache(cache_path)
    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=30)

    last_request_by_host: Dict[str, float] = {}
    counts = {"selected": len(rows), "downloaded": 0, "cached": 0, "hashed": 0, "failed": 0}
    try:
        for index, listing in enumerate(rows):
            url = str(listing.image_url)
            cached_hash = cache.get(url)
            if cached_hash:
                listing.image_phash = cached_hash
                counts["cached"] += 1
                counts["hashed"] += 1
                continue

            host = urlparse(url).netloc.lower()
            last_request = last_request_by_host.get(host)
            if last_request is not None:
                remaining = delay_seconds - (clock() - last_request)
                if remaining > 0:
                    sleep(remaining)
            last_request_by_host[host] = clock()
            counts["downloaded"] += 1
            try:
                response = client.get(
                    url,
                    headers={"User-Agent": USER_AGENTS[index % len(USER_AGENTS)]},
                )
                response.raise_for_status()
                value = perceptual_hash(response.content)
            except (httpx.HTTPError, OSError, UnidentifiedImageError, ValueError) as exc:
                logger.warning(f"Image hash failed for {listing.source}:{listing.source_id}: {exc}")
                counts["failed"] += 1
                continue

            listing.image_phash = value
            cache[url] = value
            counts["hashed"] += 1

        db.commit()
        _write_cache(cache_path, cache)
    finally:
        if owns_client:
            client.close()

    logger.info(
        "Image pHash: {hashed}/{selected} hashed ({cached} cached, "
        "{downloaded} downloaded, {failed} failed)".format(**counts)
    )
    return counts
