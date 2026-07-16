"""Tests for hidden-trap extraction (TIN-516) + TIN-515 schema fixes."""

from src.enrichment.llm_extract import ExtractedAttributes, hard_traps


def base(**overrides):
    payload = {
        "renovation_state": "unknown",
        "act16": None,
        "has_elevator": None,
        "parking": "unknown",
        "furnished": None,
    }
    payload.update(overrides)
    return payload


def test_intercardinal_exposure_accepted():
    attrs = ExtractedAttributes.model_validate(base(exposure=["southwest", "northeast"]))
    assert attrs.exposure == ["southwest", "northeast"]


def test_unknown_string_booleans_become_null():
    attrs = ExtractedAttributes.model_validate(
        base(act16="unknown", has_elevator="", furnished="null")
    )
    assert attrs.act16 is None and attrs.has_elevator is None and attrs.furnished is None


def test_old_persisted_blobs_still_validate():
    # Pre-TIN-516 blobs lack every trap field — defaults must cover them.
    attrs = ExtractedAttributes.model_validate(
        base(exposure=["south"], view="Vitosha", balcony_count=1, red_flags=[])
    )
    assert attrs.compensation_deal is False
    assert attrs.trap_flags() == []


def test_hard_traps_and_flags():
    attrs = ExtractedAttributes.model_validate(
        base(
            compensation_deal=True,
            tenanted=True,
            land_status="agricultural",
            construction_stage="act14",
        )
    )
    flags = attrs.trap_flags()
    assert flags[0] == "compensation_deal"  # hard traps first
    assert "tenanted" in flags and "land_agricultural" in flags and "stage_act14" in flags


def test_hard_traps_helper_reads_persisted_json():
    import json

    blob = json.dumps(base(ideal_parts=True, swap_only=True))
    assert hard_traps(blob) == ["ideal_parts", "swap_only"]
    assert hard_traps(None) == []
    assert hard_traps("not json") == []


def test_billing_error_aborts_batch_fast():
    # An empty credit balance fails every call identically — the batch must
    # abort on the first one, not retry per listing (2026-07-13 lesson).
    from src.enrichment.llm_extract import extract_listing_attributes

    class DeadProvider:
        name = "anthropic"
        calls = 0

        def extract(self, description):
            DeadProvider.calls += 1
            raise RuntimeError(
                "Error code: 400 - Your credit balance is too low to access the Anthropic API."
            )

    class FakeRow:
        id = 1
        description_full = "тухла, юг"

    class StubDb:
        def commit(self):
            pass

    summary = extract_listing_attributes(
        StubDb(), provider=DeadProvider(), rows=[FakeRow(), FakeRow(), FakeRow()]
    )
    assert summary["provider_dead"] is True
    assert DeadProvider.calls == 1  # aborted immediately, no per-listing retries


def test_improvised_enum_values_coerce_instead_of_failing():
    # 2026-07-16: the model answered renovation_state='completed', which the
    # strict Literal rejected — 149 of 300 billed extractions failed. Any
    # out-of-vocabulary enum value must degrade, never fail.
    attrs = ExtractedAttributes.model_validate(
        base(
            renovation_state="completed",
            parking="underground",
            construction_stage="act16",
            land_status="urban",
        )
    )
    assert attrs.renovation_state == "renovated"
    assert attrs.parking == "unknown"
    assert attrs.construction_stage == "completed"
    assert attrs.land_status == "unknown"


def test_unrecognized_gross_area_components_dropped_not_fatal():
    # 2026-07-16 run: 'internal_stairs' in gross_area_includes rejected the
    # whole billed extraction. Unknown list items must be filtered out.
    attrs = ExtractedAttributes.model_validate(
        base(gross_area_includes=["common_parts", "internal_stairs", "balcony"])
    )
    assert attrs.gross_area_includes == ["common_parts", "balcony"]


def test_overlong_free_text_truncated_not_fatal():
    # 2026-07-16 finishing pass: 'view' prose past max_length=160 rejected 65
    # billed extractions. Long strings must clip, never fail.
    attrs = ExtractedAttributes.model_validate(
        base(view="Quiet residential area " * 20, red_flags=["x" * 300] + ["ok"] * 14)
    )
    assert len(attrs.view) == 160
    assert attrs.view.endswith("...")
    assert len(attrs.red_flags) == 12
    assert len(attrs.red_flags[0]) == 160


def test_net_area_junk_string_nulled():
    attrs = ExtractedAttributes.model_validate(
        base(net_area_sqm='1</ancony_count>\n<param name="exposure">["south"]')
    )
    assert attrs.net_area_sqm is None
