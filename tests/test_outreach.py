import pytest

from src.enrichment.outreach import (
    OutreachContext,
    build_user_prompt,
    draft_inquiry,
    suggested_opening_offer,
)


def context(**overrides) -> OutreachContext:
    values = {
        "title": "Тристаен апартамент в Лозенец",
        "neighborhood": "Лозенец",
        "property_type": "apartment",
        "rooms": 3,
        "area_sqm": 92,
        "price_eur": 250000,
        "price_per_sqm_eur": 2717,
        "neighborhood_median": 3000,
        "predicted_price_per_sqm": 3100,
        "residual_pct": -12,
        "motivated_score": 70,
    }
    values.update(overrides)
    return OutreachContext(**values)


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return "Здравейте, интересувам се от тристайния апартамент в Лозенец. Удобно ли е за оглед в сряда след 18:00?"


def test_opening_offer_uses_motivation_but_protects_already_underpriced_deal():
    assert suggested_opening_offer(context()) == 230000
    assert suggested_opening_offer(context(residual_pct=-20)) == 242000


def test_opening_offer_prompt_contains_grounded_number_and_one_question_instruction():
    prompt = build_user_prompt(context(), "opening-offer")

    assert "Предложи 230000 EUR" in prompt
    assert "Медиана за квартала: 3000 EUR/кв.м" in prompt
    assert "Сигнал за мотивиран продавач: 70/100" in prompt
    assert "един конкретен въпрос" in prompt


def test_draft_inquiry_uses_one_listing_provider_call():
    provider = FakeProvider()

    draft = draft_inquiry(context(), "view-request", provider=provider)

    assert draft.count("?") == 1
    assert "Лозенец" in draft
    assert len(provider.calls) == 1
    assert "Имот: Тристаен апартамент в Лозенец" in provider.calls[0][1]


def test_draft_inquiry_fails_cleanly_when_provider_is_off():
    with pytest.raises(RuntimeError, match="provider is off"):
        draft_inquiry(context(), "availability-check", provider_name="off")
