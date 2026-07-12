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


def test_net_area_junk_string_nulled():
    attrs = ExtractedAttributes.model_validate(
        base(net_area_sqm='1</ancony_count>\n<param name="exposure">["south"]')
    )
    assert attrs.net_area_sqm is None
