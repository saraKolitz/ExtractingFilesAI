"""Tests for the confidence bands in backend/app/services/ai_service.py (CLAUDE.md section 5).

With the defaults 15 / 70: a score under 15 (including the 0 of a field the model did not find)
is dropped, 15 up to and including 70 is kept but flagged, and only a score above 70 passes clean. All values below are made up.
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


def test_the_defaults_are_15_and_70():
    assert ai.DEFAULT_CONF_EMPTY_BELOW == 15
    assert ai.DEFAULT_CONF_OK_ABOVE == 70


def test_a_missing_or_empty_setting_falls_back_on_the_default(monkeypatch):
    monkeypatch.delenv("CONF_EMPTY_BELOW", raising=False)
    assert ai._conf_threshold("CONF_EMPTY_BELOW", 1) == 1
    monkeypatch.setenv("CONF_EMPTY_BELOW", "")
    assert ai._conf_threshold("CONF_EMPTY_BELOW", 1) == 1


def test_a_setting_in_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv("CONF_OK_ABOVE", "90")
    assert ai._conf_threshold("CONF_OK_ABOVE", 70) == 90


@pytest.mark.parametrize("raw, expected", [("abc", 70), ("150", 100), ("-5", 0)])
def test_a_bad_setting_is_replaced_or_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("CONF_OK_ABOVE", raw)
    assert ai._conf_threshold("CONF_OK_ABOVE", 70) == expected


# --- the bands -------------------------------------------------------------------------------


@pytest.mark.parametrize("score", [0, 1, 5, 14])
def test_under_15_percent_the_value_is_dropped_and_the_field_stays_empty(score):
    data = result_with(first_name=score, central_numbers=score)
    ai._apply_confidence(data)
    assert data["first_name"] is None
    assert data["central_numbers"] == []
    assert data["confidence"]["first_name"] == 0  # nothing kept, nothing to be confident about
    assert {"first_name", "central_numbers"} <= set(data["fields_requiring_review"])


@pytest.mark.parametrize("score", [15, 16, 20, 45, 70])
def test_from_15_up_to_70_the_value_is_kept_and_flagged(score):
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


def test_the_boundaries_sit_exactly_at_14_15_70_71():
    data = result_with(first_name=14, last_name=15, id_number=70, received_date=71)
    ai._apply_confidence(data)
    assert data["first_name"] is None
    assert data["last_name"] == "לוי"
    assert data["id_number"] == "123456782"
    assert data["received_date"] == "20/10/2026"
    assert set(data["fields_requiring_review"]) == {"first_name", "last_name", "id_number"}


def test_the_thresholds_are_sent_along_for_the_table():
    data = result_with()
    ai._apply_confidence(data)
    assert data["thresholds"] == {"empty_below": 15, "ok_above": 70}


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
    assert f"Scores of {ai.CONF_EMPTY_BELOW} and up are all kept" in flat
    assert f"A score below {ai.CONF_EMPTY_BELOW} makes the code throw the value away" in flat
    assert f"you score {ai.CONF_OK_ABOVE} or below must also be listed" in flat
    assert f"anything from {ai.CONF_EMPTY_BELOW} up is kept" in flat
    classification = " ".join(ai.ENTITY_CLASSIFICATION_PROMPT.split())
    assert re.search(rf"below {ai.CONF_EMPTY_BELOW} makes the code discard the entity", classification)


def test_the_prompts_say_a_found_field_is_always_written_and_only_a_missing_one_is_left_empty():
    flat = " ".join(ai.PROMPT.split())
    assert "FOUND the field: ALWAYS WRITE THE VALUE" in flat
    assert "however unsure you are" in flat
    assert "Did NOT find the field" in flat and "score it 0" in flat
    # The guard against invention stays: unsure is allowed, made up is not.
    assert "never write a value that is not visible on the page" in flat
    assert "an unsure reading of what is there is fine, a completion of what is not is not" in flat
    # The old rule - leave it empty when not sure - is gone.
    assert "you did not really read it" not in flat
    assert "do not pick one just because" not in flat
    classification = " ".join(ai.ENTITY_CLASSIFICATION_PROMPT.split())
    assert "answer with it even when you are not sure" in classification
