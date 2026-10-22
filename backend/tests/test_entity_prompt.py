"""Tests for the two-step flow in backend/app/services/ai_service.py: the sender is classified first,
and the extraction prompt is then built from the general prompt plus that sender's layout notes.

No network: the Anthropic client is replaced by a scripted fake. All names and numbers are made up.
Run from the project root:  backend\\venv\\Scripts\\python.exe -m pytest
"""

import json
from types import SimpleNamespace

import pytest

from backend.app.services import ai_service as ai

ENTITY = "כללית"
EVIDENCE = "שירותי בריאות כללית"  # contains the alias "כללית"
NOTES = "TEST NOTES: the commitment number is printed top-left."
VALID_ID = "123456782"


@pytest.fixture(autouse=True)
def fixed_thresholds(monkeypatch):
    monkeypatch.setattr(ai, "CONF_EMPTY_BELOW", ai.DEFAULT_CONF_EMPTY_BELOW)
    monkeypatch.setattr(ai, "CONF_OK_ABOVE", ai.DEFAULT_CONF_OK_ABOVE)


def with_notes(monkeypatch, notes: str) -> None:
    monkeypatch.setitem(ai.ENTITY_TEMPLATES[ENTITY], "hints", notes)


# --- the prompt per sender -------------------------------------------------------------------


def test_known_sender_with_notes_gets_them(monkeypatch):
    with_notes(monkeypatch, NOTES)
    prompt = ai.build_extraction_prompt(ENTITY)
    assert NOTES in prompt
    assert ENTITY in prompt


def test_known_sender_without_notes_gets_only_the_general_instructions(monkeypatch):
    with_notes(monkeypatch, "")
    prompt = ai.build_extraction_prompt(ENTITY)
    assert "LAYOUT NOTES" not in prompt
    assert ENTITY in prompt  # it is still told who sent the form
    assert "EXTRACTION RULES" in prompt


def test_unidentified_sender_gets_the_general_prompt():
    prompt = ai.build_extraction_prompt(None)
    assert "LAYOUT NOTES" not in prompt
    assert "could not be identified" in prompt
    assert prompt == ai.PROMPT


def test_notes_of_another_sender_never_leak_in(monkeypatch):
    with_notes(monkeypatch, NOTES)
    assert NOTES not in ai.build_extraction_prompt(None)
    assert NOTES not in ai.build_extraction_prompt("מכבי")


@pytest.mark.parametrize("entity", [None, ENTITY])
def test_extraction_prompt_does_not_ask_for_the_sender(entity):
    prompt = ai.build_extraction_prompt(entity)
    assert '"sending_entity"' not in prompt
    assert "STEP 1" not in prompt
    assert "get_entity_hints" not in prompt
    for field in ai.CROP_FIELDS:
        assert f'"{field}"' in prompt


def flat(text: str) -> str:
    """The prompt on one line, so a phrase that wraps in the source can still be matched."""
    return " ".join(text.split())


@pytest.mark.parametrize("entity", [None, ENTITY])
def test_central_numbers_need_the_label_and_the_format_is_only_a_hint(entity):
    prompt = flat(ai.build_extraction_prompt(entity))
    assert 'ONLY if it is explicitly tied to the label "מרכזי"' in prompt
    assert "93xxxx or 94xxxx" in prompt
    assert "FORMAT IS ONLY A HINT" in prompt
    assert "the LABEL is what makes a number a central number" in prompt


@pytest.mark.parametrize("entity", [None, ENTITY])
def test_a_rotated_central_label_is_looked_for_but_a_label_is_still_required(entity):
    prompt = flat(ai.build_extraction_prompt(entity))
    assert "LOOK FOR THE LABEL CAREFULLY" in prompt
    assert "printed vertically, sideways or rotated" in prompt
    assert 'Whenever "מרכזי" appears near a barcode or such a number, in any orientation' in prompt
    assert "all four sides" in prompt  # the crop instruction tells it to look around the sticker
    # Finding the rotated label must not turn into accepting a number that has none.
    assert "not about accepting a number that has none" in prompt
    assert 'ONLY if it is explicitly tied to the label "מרכזי"' in prompt


@pytest.mark.parametrize("entity", [None, ENTITY])
def test_commitment_default_rule_excludes_the_other_numbers(entity):
    prompt = flat(ai.build_extraction_prompt(entity))
    assert "DEFAULT RULE" in prompt
    assert "NEVER" in prompt
    for excluded in ('a "מרכזי" number', "ID number", "case / visit / admission number", "phone number", "a date"):
        assert excluded in prompt
    assert "write the likeliest candidate" in prompt
    assert "those exclusions rule out" in prompt


def test_the_notes_of_a_sender_take_precedence_over_the_default_rule(monkeypatch):
    with_notes(monkeypatch, NOTES)
    prompt = flat(ai.build_extraction_prompt(ENTITY))
    assert "take precedence over the default rules above" in prompt
    # The default rule is stated before the notes, so "above" is true.
    assert prompt.index("DEFAULT RULE") < prompt.index(NOTES)


def test_the_hints_tool_is_gone_and_crops_cannot_target_the_sender():
    names = [tool["name"] for tool in ai.CLAUDE_TOOLS]
    assert names == ["crop_image"]
    crop_enum = ai.CLAUDE_TOOLS[0]["input_schema"]["properties"]["fields"]["items"]["enum"]
    assert "sending_entity" not in crop_enum


# --- reading the classification reply --------------------------------------------------------


def test_valid_classification_is_kept():
    reply = {"entity": ENTITY, "evidence": EVIDENCE, "confidence": 95}
    assert ai._resolve_classification(reply) == (ENTITY, EVIDENCE, 95)


def test_an_alias_is_mapped_to_the_canonical_name():
    reply = {"entity": "clalit", "evidence": "Clalit Health Services - כללית", "confidence": 90}
    assert ai._resolve_classification(reply)[0] == ENTITY


def test_quote_that_does_not_name_the_entity_clears_it():
    reply = {"entity": ENTITY, "evidence": "טופס התחייבות לבדיקה", "confidence": 99}
    assert ai._resolve_classification(reply) == (None, None, None)


def test_missing_quote_clears_it():
    assert ai._resolve_classification({"entity": ENTITY, "confidence": 99}) == (None, None, None)


def test_name_not_on_the_list_is_cleared():
    reply = {"entity": "הרשות הפלסטינאית", "evidence": "הרשות הפלסטינאית", "confidence": 99}
    assert ai._resolve_classification(reply) == (None, None, None)


def test_a_zero_score_counts_as_not_identified():
    reply = {"entity": ENTITY, "evidence": EVIDENCE, "confidence": ai.CONF_EMPTY_BELOW - 1}
    assert ai._resolve_classification(reply) == (None, None, None)


@pytest.mark.parametrize("score", [1, 5, 40])
def test_an_unsure_but_quoted_classification_is_kept(score):
    # Found-but-unsure is written (and flagged later); the quote check above is what guards it.
    reply = {"entity": ENTITY, "evidence": EVIDENCE, "confidence": score}
    assert ai._resolve_classification(reply) == (ENTITY, EVIDENCE, score)


def test_missing_confidence_keeps_the_entity_unscored():
    reply = {"entity": ENTITY, "evidence": EVIDENCE}
    assert ai._resolve_classification(reply) == (ENTITY, EVIDENCE, None)


@pytest.mark.parametrize("reply", [None, [], "כללית", {}, {"entity": None}, {"entity": 7}, {"entity": ""}])
def test_unusable_replies_mean_not_identified(reply):
    assert ai._resolve_classification(reply) == (None, None, None)


# --- the calls, with a scripted client -------------------------------------------------------


def reply_of(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeClient:
    """Returns the scripted replies in order and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.replies.pop(0)


def extraction_json(**overrides) -> str:
    data = {
        "first_name": "דנה", "last_name": "לוי", "id_number": VALID_ID,
        "central_numbers": ["1234567"], "received_date": "20/10/2026",
        "commitment_numbers": ["555"],
        "confidence": {k: 100 for k in ai.CROP_FIELDS},
        "fields_requiring_review": [],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def classification_json(entity=ENTITY, evidence=EVIDENCE, confidence=95) -> str:
    return json.dumps({"entity": entity, "evidence": evidence, "confidence": confidence}, ensure_ascii=False)


def request_text(request) -> str:
    return next(b["text"] for b in request["messages"][0]["content"] if b["type"] == "text")


def test_classify_entity_sends_the_classification_prompt(monkeypatch):
    fake = FakeClient(reply_of(classification_json()))
    monkeypatch.setattr(ai, "client", fake)
    assert ai.classify_entity(b"png") == (ENTITY, EVIDENCE, 95)
    assert request_text(fake.requests[0]) == ai.ENTITY_CLASSIFICATION_PROMPT
    assert "tools" not in fake.requests[0]


def test_classify_entity_reply_that_is_not_json_means_not_identified(monkeypatch):
    monkeypatch.setattr(ai, "client", FakeClient(reply_of("כללית")))
    assert ai.classify_entity(b"png") == (None, None, None)


def test_classify_entity_accepts_a_fenced_reply(monkeypatch):
    fenced = "```json\n" + classification_json() + "\n```"
    monkeypatch.setattr(ai, "client", FakeClient(reply_of(fenced)))
    assert ai.classify_entity(b"png")[0] == ENTITY


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_classify_entity_raises_when_claude_cannot_answer(monkeypatch, stop_reason):
    monkeypatch.setattr(ai, "client", FakeClient(reply_of("", stop_reason)))
    with pytest.raises(RuntimeError):
        ai.classify_entity(b"png")


def test_identified_sender_flows_into_the_extraction(monkeypatch):
    with_notes(monkeypatch, NOTES)
    unsure = ai.CONF_OK_ABOVE - 10  # in the flagged band, so the reviewer is pointed at the sender
    fake = FakeClient(reply_of(classification_json(confidence=unsure)), reply_of(extraction_json()))
    monkeypatch.setattr(ai, "client", fake)

    result = ai.extract_with_agent_loop(b"png")

    assert len(fake.requests) == 2  # one to classify, one to extract - nothing sent back to crop
    classify_request, extract_request = fake.requests
    assert request_text(classify_request) == ai.ENTITY_CLASSIFICATION_PROMPT
    assert request_text(extract_request) == ai.build_extraction_prompt(ENTITY)
    assert NOTES in request_text(extract_request)
    assert [t["name"] for t in extract_request["tools"]] == ["crop_image"]
    assert result["sending_entity"] == ENTITY
    assert result["confidence"]["sending_entity"] == unsure
    assert result["fields_requiring_review"] == ["sending_entity"]
    assert result["first_name"] == "דנה"


def test_a_confidently_classified_sender_passes_clean(monkeypatch):
    sure = ai.CONF_OK_ABOVE + 1
    fake = FakeClient(reply_of(classification_json(confidence=sure)), reply_of(extraction_json()))
    monkeypatch.setattr(ai, "client", fake)
    result = ai.extract_with_agent_loop(b"png")
    assert result["sending_entity"] == ENTITY
    assert result["fields_requiring_review"] == []


def test_unidentified_sender_is_extracted_with_the_general_prompt_and_flagged(monkeypatch):
    with_notes(monkeypatch, NOTES)
    fake = FakeClient(reply_of(classification_json(None, None, 0)), reply_of(extraction_json()))
    monkeypatch.setattr(ai, "client", fake)

    result = ai.extract_with_agent_loop(b"png")

    assert len(fake.requests) == 2  # an empty sender is not sent back to be cropped
    assert request_text(fake.requests[1]) == ai.PROMPT
    assert NOTES not in request_text(fake.requests[1])
    assert result["sending_entity"] is None
    assert result["confidence"]["sending_entity"] == 0
    assert "sending_entity" in result["fields_requiring_review"]
    assert result["first_name"] == "דנה"


def test_the_model_cannot_override_the_classified_sender(monkeypatch):
    fake = FakeClient(
        reply_of(classification_json()),
        reply_of(extraction_json(sending_entity="מכבי", sending_entity_evidence="מכבי")),
    )
    monkeypatch.setattr(ai, "client", fake)
    assert ai.extract_with_agent_loop(b"png")["sending_entity"] == ENTITY


# --- the JSON in the model's final reply ------------------------------------------------------

OBJECT = {"first_name": "דנה", "central_numbers": ["948205"]}


@pytest.mark.parametrize("text", [
    json.dumps(OBJECT, ensure_ascii=False),
    "```json\n" + json.dumps(OBJECT, ensure_ascii=False) + "\n```",
    "Here is the JSON you asked for:\n```json\n" + json.dumps(OBJECT, ensure_ascii=False, indent=2) + "\n```\nLet me know if you need more.",
    "All fields were found. " + json.dumps(OBJECT, ensure_ascii=False) + " Done.",
    "\n\n  " + json.dumps(OBJECT, ensure_ascii=False) + "  \n",
])
def test_the_object_is_found_whatever_is_written_around_it(text):
    assert ai._parse_json_reply(text) == OBJECT


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2, 3]", '"just a string"', "{broken", "} before {", "```\n```"])
def test_a_reply_without_a_json_object_is_a_value_error(text):
    with pytest.raises(ValueError):
        ai._parse_json_reply(text)


# --- where the central number and the commitment number sit -----------------------------------


@pytest.mark.parametrize("entity", [None, ENTITY])
def test_the_central_label_is_described_as_vertical_to_the_right_of_the_barcode(entity):
    prompt = flat(ai.build_extraction_prompt(entity))
    assert "WHERE IT USUALLY IS" in prompt
    assert 'to the RIGHT of the barcode the word "מרכזי" printed vertically (rotated 90 degrees)' in prompt
    assert "the number printed beneath the barcode" in prompt
    assert "often at the top of the page" in prompt
    # An empty answer is only allowed after looking around every barcode - not straight away.
    assert "do NOT answer null / [] for central_numbers just because you did not spot the label" in prompt
    assert "turn the crop sideways" in prompt


@pytest.mark.parametrize("entity", [None, ENTITY])
def test_a_commitment_number_with_no_label_is_a_6_to_8_digit_number_beside_a_barcode(entity):
    prompt = flat(ai.build_extraction_prompt(entity))
    assert "OFTEN NO LABEL AT ALL" in prompt
    assert "a number of 6 to 8 digits printed next to a barcode" in prompt
    assert "do not skip it because no label names it" in prompt
    # The barcode that carries the central label is told apart from the commitment's one,
    # and a patient-box number is never mistaken for it.
    assert 'the one that carries the word "מרכזי" is the central number' in prompt
    assert 'next to "ת.ז." is the ID number, whatever its length' in prompt


def test_the_commitment_rule_still_defers_to_the_notes_of_a_sender(monkeypatch):
    with_notes(monkeypatch, NOTES)
    prompt = flat(ai.build_extraction_prompt(ENTITY))
    assert prompt.index("OFTEN NO LABEL AT ALL") < prompt.index(NOTES)
    assert "take precedence over the default rules above" in prompt
