"""Validated LLM extraction of structured property attributes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Protocol

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_INPUT_USD_PER_MTOK,
    ANTHROPIC_LLM_MODEL,
    ANTHROPIC_OUTPUT_USD_PER_MTOK,
    LLM_DAILY_BUDGET_USD,
    LLM_MAX_PER_RUN,
    LLM_PROVIDER,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_MODEL,
)
from src.database.models import Listing
from src.utils.time import utc_now


class ExtractedAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exposure: List[Literal["south", "north", "east", "west"]] = Field(default_factory=list)
    view: str | None = Field(default=None, max_length=160)
    renovation_state: Literal["turnkey", "renovated", "needs_renovation", "unfinished", "unknown"]
    act16: bool | None
    has_elevator: bool | None
    parking: Literal["garage", "parking_space", "street", "none", "unknown"]
    heating_detail: str | None = Field(default=None, max_length=160)
    furnished: bool | None
    balcony_count: int | None = Field(default=None, ge=0, le=20)
    red_flags: List[str] = Field(default_factory=list, max_length=12)

    @field_validator("exposure")
    @classmethod
    def unique_exposure(cls, value):
        return list(dict.fromkeys(value))


@dataclass
class ProviderResult:
    data: Dict[str, Any]
    model: str
    cost_usd: float = 0.0


class ExtractionProvider(Protocol):
    name: str

    def extract(self, description: str) -> ProviderResult: ...


SYSTEM_PROMPT = """Extract only explicitly stated real-estate attributes from the listing description.
Use null/unknown when evidence is absent. Do not infer Act 16, elevator, parking, furnishing, or exposure.
Keep view/heating/red-flag text concise and in English. Return exactly the supplied schema."""

TOOL_NAME = "record_listing_attributes"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str = ANTHROPIC_API_KEY, model: str = ANTHROPIC_LLM_MODEL):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("anthropic SDK is not installed") from exc
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def extract(self, description: str) -> ProviderResult:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            temperature=0,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": description[:12000]}],
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": "Record validated property attributes as JSON.",
                    "input_schema": ExtractedAttributes.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        block = next((item for item in response.content if getattr(item, "type", None) == "tool_use"), None)
        if block is None:
            raise ValueError("Anthropic response did not contain forced tool JSON")
        usage = response.usage
        input_tokens = float(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = float(getattr(usage, "output_tokens", 0) or 0)
        cache_write = float(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = float(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost = estimate_anthropic_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
        )
        return ProviderResult(data=dict(block.input), model=response.model, cost_usd=cost)


class LocalProvider:
    name = "local"

    def __init__(
        self,
        base_url: str = LOCAL_LLM_BASE_URL,
        model: str = LOCAL_LLM_MODEL,
        client: httpx.Client | None = None,
    ):
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.client = client or httpx.Client(timeout=90)

    def extract(self, description: str) -> ProviderResult:
        response = self.client.post(
            self.url,
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "Return JSON matching this schema:\n"
                            f"{json.dumps(ExtractedAttributes.model_json_schema())}\n\n"
                            f"Listing:\n{description[:12000]}"
                        ),
                    },
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.removeprefix("json").strip()
        return ProviderResult(data=json.loads(content), model=self.model, cost_usd=0.0)


def build_provider(name: str = LLM_PROVIDER) -> ExtractionProvider | None:
    if name == "off":
        return None
    if name == "anthropic":
        return AnthropicProvider()
    if name == "local":
        return LocalProvider()
    raise ValueError(f"Unsupported LLM_PROVIDER: {name}")


def estimate_anthropic_cost(
    *,
    input_tokens: float,
    output_tokens: float,
    cache_write_tokens: float = 0,
    cache_read_tokens: float = 0,
) -> float:
    """Estimate Haiku API cost including 5-minute cache write/read rates."""
    return (
        input_tokens * ANTHROPIC_INPUT_USD_PER_MTOK
        + cache_write_tokens * ANTHROPIC_INPUT_USD_PER_MTOK * 1.25
        + cache_read_tokens * ANTHROPIC_INPUT_USD_PER_MTOK * 0.1
        + output_tokens * ANTHROPIC_OUTPUT_USD_PER_MTOK
    ) / 1_000_000


def extract_listing_attributes(
    db: Session,
    *,
    provider: ExtractionProvider | None = None,
    provider_name: str = LLM_PROVIDER,
    max_per_run: int = LLM_MAX_PER_RUN,
    budget_usd: float = LLM_DAILY_BUDGET_USD,
) -> Dict[str, Any]:
    """Extract and persist validated attributes with one retry per listing."""
    if provider is None:
        provider = build_provider(provider_name)
    if provider is None:
        logger.info("LLM attribute extraction skipped: provider is off")
        return {
            "provider": "off",
            "selected": 0,
            "extracted": 0,
            "failed": 0,
            "spent_usd": 0.0,
            "skipped": "provider_off",
        }

    rows = (
        db.query(Listing)
        .filter(
            Listing.is_active.is_(True),
            or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
            Listing.description_full.isnot(None),
            Listing.description_full != "",
            or_(
                Listing.llm_extracted_at.is_(None),
                Listing.enriched_at > Listing.llm_extracted_at,
            ),
        )
        .order_by(Listing.llm_extracted_at.asc(), Listing.enriched_at.asc(), Listing.id.asc())
        .limit(max_per_run)
        .all()
    )

    extracted = failed = 0
    spent = 0.0
    budget_exhausted = False
    for row in rows:
        if spent >= budget_usd:
            budget_exhausted = True
            break
        attributes = None
        result = None
        for attempt in range(2):
            try:
                result = provider.extract(row.description_full)
                spent += max(0.0, float(result.cost_usd))
                attributes = ExtractedAttributes.model_validate(result.data)
                break
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"LLM extraction failed for listing {row.id} after retry: {exc}")
                if spent >= budget_usd:
                    budget_exhausted = True
                    break
        if attributes is None or result is None:
            failed += 1
            continue

        payload = attributes.model_dump()
        row.exposure = json.dumps(payload["exposure"])
        row.renovation_state = payload["renovation_state"]
        row.act16 = payload["act16"]
        row.has_elevator = payload["has_elevator"]
        row.parking = payload["parking"]
        row.llm_extract = json.dumps(payload, ensure_ascii=False)
        row.llm_extracted_at = utc_now()
        row.llm_model_used = result.model
        extracted += 1
        if extracted % 25 == 0:
            db.commit()
    db.commit()

    return {
        "provider": provider.name,
        "selected": len(rows),
        "extracted": extracted,
        "failed": failed,
        "spent_usd": round(spent, 6),
        "budget_exhausted": budget_exhausted,
    }
