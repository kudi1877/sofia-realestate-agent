import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Listing
from src.enrichment.llm_extract import (
    LocalProvider,
    ProviderResult,
    estimate_anthropic_cost,
    extract_listing_attributes,
)


NOW = datetime(2026, 7, 12, 12, 0)
FIXTURE = Path(__file__).parent / "fixtures" / "real_descriptions.json"


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, description: str, **overrides) -> Listing:
    values = {
        "source": "test",
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "area_sqm": 50,
        "price_eur": 100000,
        "price_per_sqm_eur": 2000,
        "description_full": description,
        "enriched_at": NOW,
        "is_active": True,
        "is_duplicate": False,
    }
    values.update(overrides)
    return Listing(**values)


VALID = {
    "exposure": ["south", "east", "south"],
    "view": "mountain",
    "renovation_state": "renovated",
    "act16": True,
    "has_elevator": True,
    "parking": "parking_space",
    "heating_detail": "central heating",
    "furnished": True,
    "balcony_count": 1,
    "red_flags": [],
}


class MockProvider:
    name = "mock"

    def __init__(self, responses=None, cost=0.001):
        self.responses = list(responses or [])
        self.cost = cost
        self.calls = 0

    def extract(self, description):
        self.calls += 1
        data = self.responses.pop(0) if self.responses else VALID
        return ProviderResult(data=data, model="mock-model", cost_usd=self.cost)


def test_five_real_descriptions_extract_and_store_typed_fields():
    descriptions = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(descriptions) == 5
    db = session()
    db.add_all([listing(str(index), description) for index, description in enumerate(descriptions)])
    db.commit()
    provider = MockProvider()

    summary = extract_listing_attributes(db, provider=provider, max_per_run=5, budget_usd=2)

    rows = db.query(Listing).order_by(Listing.id).all()
    assert summary["extracted"] == 5
    assert provider.calls == 5
    assert all(json.loads(row.exposure) == ["south", "east"] for row in rows)
    assert all(row.renovation_state == "renovated" for row in rows)
    assert all(row.act16 is True and row.has_elevator is True for row in rows)
    assert all(row.parking == "parking_space" for row in rows)
    assert all(json.loads(row.llm_extract)["view"] == "mountain" for row in rows)
    assert all(row.llm_model_used == "mock-model" for row in rows)


def test_invalid_json_is_retried_once_then_stored():
    db = session()
    db.add(listing("retry", "Act 16 and elevator are explicitly present."))
    db.commit()
    provider = MockProvider(responses=[{"exposure": ["south"]}, VALID])

    summary = extract_listing_attributes(db, provider=provider)

    assert provider.calls == 2
    assert summary["extracted"] == 1
    assert summary["failed"] == 0


def test_daily_budget_counts_provider_calls_and_stops_before_next_listing():
    db = session()
    db.add_all([listing(str(index), "Real description") for index in range(3)])
    db.commit()
    provider = MockProvider(cost=1.1)

    summary = extract_listing_attributes(db, provider=provider, budget_usd=2)

    assert provider.calls == 2
    assert summary["extracted"] == 2
    assert summary["spent_usd"] == 2.2
    assert summary["budget_exhausted"] is True


def test_provider_off_skips_cleanly():
    summary = extract_listing_attributes(session(), provider_name="off")

    assert summary["skipped"] == "provider_off"
    assert summary["extracted"] == 0


class FakeLocalClient:
    def __init__(self):
        self.url = None
        self.payload = None

    def post(self, url, json):
        self.url = url
        self.payload = json
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": __import__("json").dumps(VALID)}}]},
            request=httpx.Request("POST", url),
        )


def test_local_provider_uses_openai_compatible_json_mode():
    client = FakeLocalClient()
    provider = LocalProvider(base_url="http://localhost:11434/v1", model="local-test", client=client)

    result = provider.extract("A real description")

    assert client.url == "http://localhost:11434/v1/chat/completions"
    assert client.payload["response_format"] == {"type": "json_object"}
    assert result.model == "local-test"
    assert result.data["parking"] == "parking_space"


def test_anthropic_cost_estimate_includes_prompt_cache_rates():
    assert estimate_anthropic_cost(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    ) == 7.35
