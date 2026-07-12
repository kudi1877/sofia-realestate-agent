"""Draft-only Bulgarian buyer outreach using the TIN-507 provider clients.

This module intentionally exposes no send function. It provides prompt and
provider adapters for one listing at a time; delivery remains a human action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.config import LLM_PROVIDER
from src.enrichment.llm_extract import (
    AnthropicProvider,
    ExtractionProvider,
    LocalProvider,
    build_provider,
)

OutreachIntent = Literal["view-request", "availability-check", "opening-offer"]


class OutreachContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    neighborhood: str = Field(max_length=100)
    property_type: str = Field(max_length=40)
    rooms: int | None = Field(default=None, ge=0, le=30)
    area_sqm: float | None = Field(default=None, gt=0, le=10000)
    price_eur: float = Field(gt=0)
    price_per_sqm_eur: float | None = Field(default=None, gt=0)
    neighborhood_median: float | None = Field(default=None, gt=0)
    predicted_price_per_sqm: float | None = Field(default=None, gt=0)
    residual_pct: float | None = None
    motivated_score: int = Field(default=0, ge=0, le=100)


class OutreachTextProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


SYSTEM_PROMPT = """Пишеш кратки и естествени запитвания за имоти на български.
Пиши 2-4 изречения, без измислени факти, без натиск и без брокерски жаргон.
Спомени конкретния имот и включи точно един въпросителен знак.
Върни само готовия текст на съобщението, без заглавие или обяснения."""


@dataclass
class AnthropicOutreachProvider:
    provider: AnthropicProvider
    name: str = "anthropic"

    def complete(self, system: str, user: str) -> str:
        response = self.provider.client.messages.create(
            model=self.provider.model,
            max_tokens=350,
            temperature=0.3,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        block = next(
            (item for item in response.content if getattr(item, "type", None) == "text"),
            None,
        )
        if block is None:
            raise ValueError("Anthropic outreach response had no text")
        return str(block.text).strip()


@dataclass
class LocalOutreachProvider:
    provider: LocalProvider
    name: str = "local"

    def complete(self, system: str, user: str) -> str:
        response = self.provider.client.post(
            self.provider.url,
            json={
                "model": self.provider.model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"]).strip()


def build_outreach_provider(name: str = LLM_PROVIDER) -> OutreachTextProvider | None:
    provider: ExtractionProvider | None = build_provider(name)
    if provider is None:
        return None
    if isinstance(provider, AnthropicProvider):
        return AnthropicOutreachProvider(provider)
    if isinstance(provider, LocalProvider):
        return LocalOutreachProvider(provider)
    raise TypeError(f"Unsupported outreach provider: {type(provider).__name__}")


def suggested_opening_offer(context: OutreachContext) -> int:
    """Return a restrained, rounded offer anchored to ask and market context."""
    discount = 0.05
    if context.motivated_score >= 60:
        discount = 0.08
    if context.residual_pct is not None and context.residual_pct <= -15:
        discount = min(discount, 0.03)
    elif (
        context.neighborhood_median
        and context.price_per_sqm_eur
        and context.price_per_sqm_eur > context.neighborhood_median * 1.1
    ):
        discount = max(discount, 0.08)
    raw = context.price_eur * (1 - discount)
    return max(1000, int(round(raw / 1000) * 1000))


def build_user_prompt(context: OutreachContext, intent: OutreachIntent) -> str:
    facts = [
        f"Имот: {context.title}",
        f"Квартал: {context.neighborhood}",
        f"Тип: {context.property_type}",
        f"Цена: {context.price_eur:.0f} EUR",
    ]
    if context.rooms is not None:
        facts.append(f"Стаи: {context.rooms}")
    if context.area_sqm is not None:
        facts.append(f"Площ: {context.area_sqm:.0f} кв.м")
    if context.price_per_sqm_eur is not None:
        facts.append(f"Цена на кв.м: {context.price_per_sqm_eur:.0f} EUR")
    if context.neighborhood_median is not None:
        facts.append(f"Медиана за квартала: {context.neighborhood_median:.0f} EUR/кв.м")
    if context.predicted_price_per_sqm is not None:
        facts.append(f"Моделна очаквана цена: {context.predicted_price_per_sqm:.0f} EUR/кв.м")
    if context.residual_pct is not None:
        facts.append(f"Отклонение от модела: {context.residual_pct:.1f}%")
    facts.append(f"Сигнал за мотивиран продавач: {context.motivated_score}/100")

    instructions = {
        "view-request": "Поискай оглед и задай един конкретен въпрос за удобен ден и час.",
        "availability-check": "Провери дали обявата е актуална и задай един конкретен въпрос дали имотът още е свободен.",
        "opening-offer": (
            f"Предложи {suggested_opening_offer(context)} EUR и задай един конкретен въпрос "
            "дали продавачът би обсъдил тази сума при бърза организация."
        ),
    }
    return "\n".join([*facts, "", instructions[intent]])


def draft_inquiry(
    context: OutreachContext,
    intent: OutreachIntent,
    *,
    provider: OutreachTextProvider | None = None,
    provider_name: str = LLM_PROVIDER,
) -> str:
    provider = provider or build_outreach_provider(provider_name)
    if provider is None:
        raise RuntimeError("LLM provider is off")
    return provider.complete(SYSTEM_PROMPT, build_user_prompt(context, intent)).strip()
