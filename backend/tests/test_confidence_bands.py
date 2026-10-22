"""Tests for the confidence bands in backend/app/services/ai_service.py (CLAUDE.md section 5).

With the defaults 20 / 70: a score below 20 is dropped, 20 up to and including 70 is kept but
flagged, and only a score above 70 passes clean. All values below are made up.
Run from the project root:  backend\\venv\\Scripts\\python.exe -m pytest
"""

import pytest

from backend.app.services import ai_service as ai


@pytest.fixture(autouse=True)
def default_thresholds(monkeypatch):
    # Independent of whatever CONF_* the developer's own .env sets.
    monkeypatch.setattr(ai, "CONF_EMPTY_BELOW", ai.DEFAULT_CONF_EMPTY_BELOW)
    monkeypatch.setattr(ai, "CONF_OK_ABOVE", ai.DEFAULT_CONF_OK_ABOVE)


def result_with(**scores) -> dict:
    """A model reply: every field filled, scored as given (fields not given score 100)."""
    data = {
        "first_name": "דנה", "last_name": "לוי", "id_number": "123456782",
        "central_numbers": ["1234567"], "received_date": "20/10/2026", "sending_entity": "כללית",
        "commitment_numbers": ["555"], "fields_requiring_review": [],
    }
    data["confidence"] = {field: 100 for field in ai.DATA_FIELDS} | scores
    return data


# --- the defaults and how .env overrides them ------------------------------------------------


def test_the_defaults_are_20_and_70():
    assert ai.DEFAULT_CONF_EMPTY_BELOW == 20
    assert ai.DEFAULT_CONF_OK_ABOVE == 70


def test_a_missing_or_empty_setting_falls_back_on_the_default(monkeypatch):
    monkeypatch.delenv("CONF_EMPTY_BELOW", raising=False)
    assert ai._conf_threshold("CONF_EMPTY_BELOW", 20) == 20
    monkeypatch.setenv("CONF_EMPTY_BELOW", "")
    assert ai._conf_threshold("CONF_EMPTY_BELOW", 20) == 20


def test_a_setting_in_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv("CONF_OK_ABOVE", "90")
    assert ai._conf_threshold("CONF_OK_ABOVE", 70) == 90


@pytest.mark.parametrize("raw, expected", [("abc", 70), ("150", 100), ("-5", 0)])
def test_a_bad_setting_is_replaced_or_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("CONF_OK_ABOVE", raw)
    assert ai._conf_threshold("CONF_OK_ABOVE", 70) == expected


# --- the bands -------------------------------------------------------------------------------


def test_below_the_lower_threshold_the_value_is_dropped_and_flagged():
    data = result_with(first_name=19, central_numbers=19)
    ai._apply_confidence(data)
    assert data["first_name"] is None
    assert data["central_numbers"] == []
    assert data["confidence"]["first_name"] == 0  # nothing kept, nothing to be confident about
    assert {"first_name", "central_numbers"} <= set(data["fields_requiring_review"])


@pytest.mark.parametrize("score", [20, 45, 70])
def test_from_20_up_to_70_the_value_is_kept_and_flagged(score):
    data = result_with(last_name=score)
    ai._apply_confidence(data)
    assert data["last_name"] == "לוי"
    assert data["confidence"]["last_name"] == score
    assert data["fields_requiring_review"] == ["last_name"]


@pytest.mark.parametrize("score", [71, 85, 100])
def test_above_70_the_value_passes_clean(score):
    data = result_with(last_name=score)
    ai._apply_confidence(data)
    assert data["last_name"] == "לוי"
    assert data["fields_requiring_review"] == []


def test_the_boundaries_sit_exactly_at_19_20_70_71():
    data = result_with(first_name=19, last_name=20, id_number=70, received_date=71)
    ai._apply_confidence(data)
    assert data["first_name"] is None
    assert data["last_name"] == "לוי"
    assert data["id_number"] == "123456782"
    assert data["received_date"] == "20/10/2026"
    assert set(data["fields_requiring_review"]) == {"first_name", "last_name", "id_number"}


def test_the_thresholds_are_sent_along_for_the_table():
    data = result_with()
    ai._apply_confidence(data)
    assert data["thresholds"] == {"empty_below": 20, "ok_above": 70}


def test_an_empty_field_is_flagged_with_score_zero():
    data = result_with(commitment_numbers=0)
    data["commitment_numbers"] = []
    ai._apply_confidence(data)
    assert data["confidence"]["commitment_numbers"] == 0
    assert data["fields_requiring_review"] == ["commitment_numbers"]


def test_a_filled_field_without_a_score_is_flagged():
    data = result_with()
    del data["confidence"]["last_name"]
    ai._apply_confidence(data)
    assert data["last_name"] == "לוי"
    assert data["fields_requiring_review"] == ["last_name"]


def test_no_scores_at_all_flags_every_filled_field():
    data = result_with()
    data["confidence"] = None
    ai._apply_confidence(data)
    assert set(data["fields_requiring_review"]) == set(ai.DATA_FIELDS)


# --- the model's own list does not override the scores ----------------------------------------


def test_a_field_the_model_doubted_but_scored_above_70_passes_clean():
    data = result_with(last_name=85)
    data["fields_requiring_review"] = ["last_name", "first_name", "sending_entity_evidence"]
    ai._apply_confidence(data)
    assert data["fields_requiring_review"] == []


def test_the_flags_are_rebuilt_on_every_pass():
    data = result_with(last_name=40)
    ai._apply_confidence(data)
    ai._apply_confidence(data)  # the agent loop can run it again after a reminder
    assert data["fields_requiring_review"] == ["last_name"]


# --- the prompts state the same numbers the code applies -------------------------------------


def test_the_prompts_carry_the_configured_thresholds():
    # The module-level prompts are built at import, from the real .env, so compare to what it set.
    import re

    for prompt in (ai.PROMPT, ai.ENTITY_CLASSIFICATION_PROMPT):
        assert "<<" not in prompt
    flat = " ".join(ai.PROMPT.split())
    assert f"{ai.CONF_EMPTY_BELOW} or more: WRITE THE VALUE" in flat
    assert f"Below {ai.CONF_EMPTY_BELOW}: you did not really read it" in flat
    assert f"you score {ai.CONF_OK_ABOVE} or below must also be listed" in flat
    assert f"anything from {ai.CONF_EMPTY_BELOW} up is kept" in flat
    assert re.search(rf"Below {ai.CONF_EMPTY_BELOW} means you did not", " ".join(ai.ENTITY_CLASSIFICATION_PROMPT.split()))
