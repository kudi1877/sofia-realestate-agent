from datetime import datetime
from io import BytesIO

import httpx
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing
from src.enrichment.image_hash import hash_listing_images


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, image_url: str) -> Listing:
    return Listing(
        source="test",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        image_url=image_url,
        listing_kind="sale",
        neighborhood="Люлин",
        property_type="apartment",
        area_sqm=50,
        price_eur=100000,
        price_per_sqm_eur=2000,
        first_seen=datetime(2026, 7, 12),
        is_active=True,
        is_duplicate=False,
    )


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), (40, 120, 200)).save(output, format="PNG")
    return output.getvalue()


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.value += seconds


class FakeClient:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls = []

    def get(self, url: str, **_kwargs):
        self.calls.append((url, self.clock()))
        return httpx.Response(
            200,
            content=png_bytes(),
            request=httpx.Request("GET", url),
        )

    def close(self):
        pass


def test_image_hash_cache_avoids_duplicate_download_and_throttles_same_host(tmp_path):
    db = session()
    db.add_all(
        [
            listing("one", "https://images.example.test/shared.png"),
            listing("two", "https://images.example.test/shared.png"),
            listing("three", "https://images.example.test/other.png"),
        ]
    )
    db.commit()
    clock = FakeClock()
    client = FakeClient(clock)

    summary = hash_listing_images(
        db,
        max_per_run=3,
        delay_seconds=1.5,
        cache_path=tmp_path / "hashes.json",
        client=client,
        sleep=clock.sleep,
        clock=clock,
    )

    assert summary == {"selected": 3, "downloaded": 2, "cached": 1, "hashed": 3, "failed": 0}
    assert [called_at for _url, called_at in client.calls] == [0.0, 1.5]
    assert clock.sleeps == [1.5]
    hashes = {row.image_phash for row in db.query(Listing).all()}
    assert len(hashes) == 1
    assert len(next(iter(hashes))) == 16

    for row in db.query(Listing).all():
        row.image_phash = None
    db.commit()
    cached_client = FakeClient(clock)

    cached = hash_listing_images(
        db,
        max_per_run=3,
        cache_path=tmp_path / "hashes.json",
        client=cached_client,
        sleep=clock.sleep,
        clock=clock,
    )

    assert cached["cached"] == 3
    assert cached["downloaded"] == 0
    assert cached_client.calls == []
