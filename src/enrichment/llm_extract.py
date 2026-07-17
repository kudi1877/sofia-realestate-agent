"""Validated LLM extraction of structured property attributes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Protocol

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from src.config import (
    ANOMALY_ZSCORE_THRESHOLD,
    ANTHROPIC_API_KEY,
    ANTHROPIC_INPUT_USD_PER_MTOK,
    ANTHROPIC_LLM_MODEL,
    ANTHROPIC_OUTPUT_USD_PER_MTOK,
    LLM_DAILY_BUDGET_USD,
    LLM_MAX_PER_RUN,
    LLM_PROVIDER,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_MODEL,
    MOONSHOT_API_KEY,
    MOONSHOT_BASE_URL,
    MOONSHOT_CACHED_INPUT_USD_PER_MTOK,
    MOONSHOT_INPUT_USD_PER_MTOK,
    MOONSHOT_MODEL,
    MOONSHOT_OUTPUT_USD_PER_MTOK,
)
from src.database.models import Alert, Listing
from src.utils.time import utc_now


def _is_deal_clause():
    """True for listings an underpriced alert already flagged."""
    return exists().where(
        Alert.listing_id == Listing.id,
        Alert.alert_type == "underpriced",
        Alert.zscore <= ANOMALY_ZSCORE_THRESHOLD,
    )


Exposure = Literal[
    "south", "north", "east", "west",
    "southeast", "southwest", "northeast", "northwest",
]

# Trap keys that disqualify a listing from the deal feed outright: the "price"
# is not a price for the whole, unencumbered property.
HARD_TRAP_KEYS = ("compensation_deal", "ideal_parts", "swap_only", "building_right_only")

# Softer traps surfaced as warnings on the card/detail view.
SOFT_TRAP_KEYS = ("non_residential_status", "encumbrance", "tenanted")


class ExtractedAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exposure: List[Exposure] = Field(default_factory=list)
    view: str | None = Field(default=None, max_length=160)
    renovation_state: Literal["turnkey", "renovated", "needs_renovation", "unfinished", "unknown"]
    act16: bool | None
    has_elevator: bool | None
    parking: Literal["garage", "parking_space", "street", "none", "unknown"]
    heating_detail: str | None = Field(default=None, max_length=160)
    furnished: bool | None
    balcony_count: int | None = Field(default=None, ge=0, le=20)
    red_flags: List[str] = Field(default_factory=list, max_length=12)

    # ── Hidden-trap detection (TIN-516) ──────────────────────────────────────
    # Net living area when the text distinguishes it from the headline m²
    # (чиста площ vs обща/застроена площ incl. balconies/common parts).
    net_area_sqm: float | None = Field(default=None, gt=5, lt=2000)
    gross_area_includes: List[
        Literal["common_parts", "balcony", "terrace", "attic", "basement", "garage", "yard"]
    ] = Field(default_factory=list)
    # "Plot for sale" actually seeking builder compensation (обезщетение).
    compensation_deal: bool = False
    # Selling идеални части — a share of the property, not the whole.
    ideal_parts: bool = False
    # Право на строеж only — building right without the land.
    building_right_only: bool = False
    # Статут на ателие/таван/офис — priced like a home, legally not a dwelling.
    non_residential_status: bool = False
    construction_stage: Literal["completed", "act15", "act14", "off_plan", "unknown"] = "unknown"
    # С наематели — tenant in place.
    tenanted: bool = False
    # Възбрана/ипотека/тежести mentioned in the text.
    encumbrance: bool = False
    # Замяна — swap offer rather than a sale.
    swap_only: bool = False
    # For plots: земеделска земя / извън регулация needs status conversion.
    land_status: Literal["regulated", "agricultural", "unregulated", "unknown"] = "unknown"

    @field_validator("exposure", "red_flags", "gross_area_includes", mode="before")
    @classmethod
    def null_list_to_empty(cls, value):
        # Kimi answers null where the schema wants an empty list — 9 of 40
        # billed extractions failed on this in the 2026-07-17 A/B run.
        return [] if value is None else value

    @field_validator("exposure")
    @classmethod
    def unique_exposure(cls, value):
        return list(dict.fromkeys(value))

    @field_validator("act16", "has_elevator", "furnished", mode="before")
    @classmethod
    def unknown_to_null(cls, value):
        # Models frequently answer the string "unknown" for nullable booleans
        # (14 of 300 extractions failed on this in the first run, TIN-515).
        if isinstance(value, str) and value.strip().lower() in ("unknown", "null", ""):
            return None
        return value

    @field_validator("net_area_sqm", "balcony_count", mode="before")
    @classmethod
    def junk_numeric_to_null(cls, value):
        # Harden against malformed tool output fragments observed in run 1.
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned.replace(".", "", 1).isdigit():
                return None
        return value

    @field_validator("view", "heating_detail", mode="before")
    @classmethod
    def clip_long_text(cls, value):
        # The model narrates past max_length=160 ('view' listing every nearby
        # school and mall — 65 rejected extractions on the 2026-07-16
        # finishing pass). Truncated text beats a paid-for validation failure.
        if isinstance(value, str) and len(value) > 160:
            return value[:157] + "..."
        return value

    @field_validator("red_flags", mode="before")
    @classmethod
    def clip_red_flags(cls, value):
        # Same defence for the list cap (max 12 items).
        if isinstance(value, list):
            return [str(item)[:157] + "..." if len(str(item)) > 160 else item for item in value[:12]]
        return value

    @field_validator("gross_area_includes", mode="before")
    @classmethod
    def drop_unrecognized_area_components(cls, value):
        # Same failure mode as unrecognized_enum_to_unknown, list form: the
        # model volunteers items like 'internal_stairs' (seen on the 2026-07-16
        # run) and the whole billed extraction is rejected. Keep what fits.
        allowed = {"common_parts", "balcony", "terrace", "attic", "basement", "garage", "yard"}
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item.strip().lower() in allowed]
        return value

    @field_validator("renovation_state", "parking", "construction_stage", "land_status", mode="before")
    @classmethod
    def unrecognized_enum_to_unknown(cls, value, info):
        # Models improvise values outside the Literal ('completed' for
        # renovation_state failed 149 of 300 billed extractions on the
        # 2026-07-16 nightly — ~$1 paid for rejected answers). A degraded
        # 'unknown' beats a paid-for validation failure. Known synonyms are
        # mapped per field; anything else coerces to 'unknown'.
        if not isinstance(value, str):
            return value
        cleaned = value.strip().lower()
        synonyms = {
            "renovation_state": {"completed": "renovated", "new": "turnkey", "good": "renovated"},
            "construction_stage": {"act16": "completed", "act 16": "completed", "na_zeleno": "off_plan"},
        }.get(info.field_name, {})
        cleaned = synonyms.get(cleaned, cleaned)
        allowed = {
            "renovation_state": {"turnkey", "renovated", "needs_renovation", "unfinished", "unknown"},
            "parking": {"garage", "parking_space", "street", "none", "unknown"},
            "construction_stage": {"completed", "act15", "act14", "off_plan", "unknown"},
            "land_status": {"regulated", "agricultural", "unregulated", "unknown"},
        }[info.field_name]
        return cleaned if cleaned in allowed else "unknown"

    def trap_flags(self) -> List[str]:
        hard = [key for key in HARD_TRAP_KEYS if getattr(self, key)]
        soft = [key for key in SOFT_TRAP_KEYS if getattr(self, key)]
        if self.land_status in ("agricultural", "unregulated"):
            soft.append(f"land_{self.land_status}")
        if self.construction_stage in ("act14", "act15", "off_plan"):
            soft.append(f"stage_{self.construction_stage}")
        return hard + soft


def hard_traps(llm_extract_json: str | None) -> List[str]:
    """Hard disqualifiers from a persisted llm_extract JSON blob (exporter use)."""
    if not llm_extract_json:
        return []
    try:
        payload = json.loads(llm_extract_json)
    except (TypeError, ValueError):
        return []
    return [key for key in HARD_TRAP_KEYS if payload.get(key)]


@dataclass
class ProviderResult:
    data: Dict[str, Any]
    model: str
    cost_usd: float = 0.0


class ExtractionProvider(Protocol):
    name: str

    def extract(self, description: str) -> ProviderResult: ...


SYSTEM_PROMPT = """Extract only explicitly stated real-estate attributes from the Bulgarian listing description.
Use null/unknown when evidence is absent. Do not infer Act 16, elevator, parking, furnishing, or exposure.
Keep view/heating/red-flag text concise and in English. Return exactly the supplied schema.

Hunt for HIDDEN TRAPS a buyer would want flagged (set false only when truly absent):
- net_area_sqm: if the text distinguishes чиста/жилищна площ from the headline обща/застроена площ (which may include балкони, тераси, общи части, таван, мазе), record the NET living area and list what the gross figure includes in gross_area_includes.
- compensation_deal: the seller seeks обезщетение (builder compensation / срещу обезщетение) rather than a straight cash sale.
- ideal_parts: TRUE only when the SALE OBJECT is a fractional share — "продава/прехвърля идеални части от имота/апартамента", a co-ownership share. FALSE for the normal ownership note every Bulgarian flat carries: "идеални части от земята/парцела/сградата/общите части" that simply come WITH the whole apartment. If the ad sells a whole apartment (X-стаен апартамент), ideal_parts is FALSE even if it mentions идеални части от земята.
- building_right_only: право на строеж / отстъпено право на строеж without land ownership.
- non_residential_status: статут на ателие, таван, офис or similar — not legally a dwelling (жилище).
- construction_stage: акт 14 / акт 15 = act14/act15; на зелено / в строеж without a completion act = off_plan; акт 16 or existing old building = completed.
- tenanted: с наематели / отдаден под наем — tenant currently in place.
- encumbrance: възбрана, ипотека, тежести mentioned.
- swap_only: замяна — the owner wants a swap, not (only) a sale.
- land_status (plots): в регулация = regulated; земеделска земя / нива = agricultural; извън регулация = unregulated.
Anything else suspicious goes in red_flags."""

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


class MoonshotProvider:
    """Kimi via Moonshot's OpenAI-compatible chat API (LLM_PROVIDER=moonshot)."""

    name = "moonshot"

    def __init__(
        self,
        api_key: str = MOONSHOT_API_KEY,
        base_url: str = MOONSHOT_BASE_URL,
        model: str = MOONSHOT_MODEL,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise ValueError("MOONSHOT_API_KEY is not configured")
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.client = client or httpx.Client(
            timeout=90, headers={"Authorization": f"Bearer {api_key}"}
        )

    def extract(self, description: str) -> ProviderResult:
        response = self.client.post(
            self.url,
            json={
                "model": self.model,
                # Current Kimi models are reasoning models: default mode burns
                # ~1000-1500 thinking tokens and 30-50s per listing. Probed
                # 2026-07-17: reasoning_effort "none" switches to direct
                # answers and then the API mandates temperature 0.6.
                "reasoning_effort": "none",
                "temperature": 0.6,
                "max_tokens": 800,
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
        payload = response.json()
        content = payload["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.removeprefix("json").strip()
        usage = payload.get("usage") or {}
        prompt_tokens = float(usage.get("prompt_tokens") or 0)
        cached_tokens = float(usage.get("cached_tokens") or 0)
        completion_tokens = float(usage.get("completion_tokens") or 0)
        cost = (
            (prompt_tokens - cached_tokens) * MOONSHOT_INPUT_USD_PER_MTOK
            + cached_tokens * MOONSHOT_CACHED_INPUT_USD_PER_MTOK
            + completion_tokens * MOONSHOT_OUTPUT_USD_PER_MTOK
        ) / 1_000_000
        return ProviderResult(
            data=json.loads(content),
            model=str(payload.get("model") or self.model),
            cost_usd=cost,
        )


def build_provider(name: str = LLM_PROVIDER) -> ExtractionProvider | None:
    if name == "off":
        return None
    if name == "anthropic":
        return AnthropicProvider()
    if name == "moonshot":
        return MoonshotProvider()
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
    rows: List[Listing] | None = None,
) -> Dict[str, Any]:
    """Extract and persist validated attributes with one retry per listing.

    `rows` lets a caller (e.g. the TIN-516 backfill) supply its own ordered,
    availability-gated candidate list; default selection is unchanged.
    """
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

    rows = rows if rows is not None else (
        db.query(Listing)
        .filter(
            Listing.is_active.is_(True),
            or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
            # olx/bazar/alo have no detail fetcher, so their text lives in the
            # short `description` column — description_full alone silently
            # excluded them from every AI read (found via listing 35722, an
            # olx ad whose 282 m² headline hides a 115 m² apartment).
            or_(
                and_(Listing.description_full.isnot(None), Listing.description_full != ""),
                and_(Listing.description.isnot(None), Listing.description != ""),
            ),
            or_(
                Listing.llm_extracted_at.is_(None),
                Listing.enriched_at > Listing.llm_extracted_at,
            ),
        )
        # Deals first, then newest. Reading a listing costs money and only
        # pays off where you'd act: a trap on an ad you'd never open is worth
        # nothing. Oldest-first ordering spent the whole daily budget on
        # backlog every night instead of on today's deals.
        .order_by(_is_deal_clause().desc(), Listing.first_seen.desc(), Listing.id.desc())
        .limit(max_per_run)
        .all()
    )

    extracted = failed = 0
    spent = 0.0
    budget_exhausted = False
    provider_dead = False
    for row in rows:
        if spent >= budget_usd or provider_dead:
            budget_exhausted = True
            break
        attributes = None
        result = None
        for attempt in range(2):
            try:
                result = provider.extract(row.description_full or row.description)
                spent += max(0.0, float(result.cost_usd))
                attributes = ExtractedAttributes.model_validate(result.data)
                break
            except Exception as exc:
                # Account-level failures (no credits, invalid key) hit every
                # subsequent call identically — abort the whole batch instead
                # of burning hours retrying per listing (the 2026-07-13 backlog
                # run wasted ~5,400 attempts against an empty credit balance).
                message = str(exc).lower()
                if any(
                    marker in message
                    for marker in (
                        "credit balance",
                        "authentication",
                        "invalid x-api-key",
                        # Moonshot phrasings for the same account-level failures
                        "401 unauthorized",
                        "insufficient balance",
                        "account is suspended",
                    )
                ):
                    logger.error(f"LLM provider unusable, aborting batch: {exc}")
                    provider_dead = True
                    break
                if attempt == 1:
                    logger.warning(f"LLM extraction failed for listing {row.id} after retry: {exc}")
                if spent >= budget_usd:
                    budget_exhausted = True
                    break
        if provider_dead:
            failed += 1
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
        "provider_dead": provider_dead,
    }
