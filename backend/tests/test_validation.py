"""Business-rule tests for backend/app/services/validation.py (CLAUDE.md sections 4, 5 and 6).

Run from the project root:  backend\\.venv\\Scripts\\python.exe -m pytest
All names and numbers below are made up; nothing here touches the sample forms.
"""

from datetime import date

import pytest

from backend.app.services import validation as v

TODAY = date(2026, 10, 21)
VALID_ID = "123456782"  # passes the Israeli ID check digit
OK_ABOVE = 90


def extraction(**fields) -> dict:
    """A clean extraction result in the shape the model returns, overridden per test."""
    data = {
        "first_name": "דנה",
        "last_name": "לוי",
        "id_number": VALID_ID,
        "central_numbers": ["1234567"],
        "received_date": "20/10/2026",
        "sending_entity": "כללית",
        "commitment_numbers": ["555"],
        "confidence": {k: 100 for k in v_fields()},
        "fields_requiring_review": [],
    }
    data.update(fields)
    return data


def v_fields():
    return ["first_name", "last_name", "id_number", "central_numbers",
            "received_date", "sending_entity", "commitment_numbers"]


def run(**fields) -> dict:
    data = extraction(**fields)
    v.validate_extraction(data, today=TODAY, ok_above=OK_ABOVE)
    return data


# ---------------------------------------------------------------- 1. ID number

class TestIdNumber:
    @pytest.mark.parametrize("value", [VALID_ID, "000000018", "000000026"])
    def test_valid_nine_digit_ids_pass(self, value):
        assert v.validate_id_number(value) == []

    def test_leading_zeros_are_kept_and_counted(self):
        assert v.validate_id_number("000000018") == []

    @pytest.mark.parametrize("value", ["11223344", "1234567", "1"])
    def test_too_short_is_flagged_not_padded(self, value):
        assert v.validate_id_number(value) == [v.ID_WRONG_LENGTH]

    def test_too_long_is_flagged(self):
        assert v.validate_id_number("1234567821") == [v.ID_WRONG_LENGTH]

    def test_nine_digits_with_wrong_check_digit_is_flagged(self):
        assert v.validate_id_number("123456789") == [v.ID_BAD_CHECKSUM]

    @pytest.mark.parametrize("value", ["12345678a", "12-456782", "1234 5678", "١٢٣٤٥٦٧٨٢"])
    def test_non_digits_are_flagged(self, value):
        assert v.validate_id_number(value) == [v.ID_NOT_DIGITS]

    def test_the_sample_forms_eight_digit_id_goes_to_review_unchanged(self):
        # CLAUDE.md decision 2: the length check stays hard, and the value is not "fixed".
        data = run(id_number="11223344")
        assert data["id_number"] == "11223344"
        assert "id_number" in data["fields_requiring_review"]
        assert data["validation_errors"]["id_number"] == [v.ID_WRONG_LENGTH]

    def test_valid_id_is_not_flagged(self):
        data = run()
        assert "id_number" not in data["fields_requiring_review"]
        assert "validation_errors" not in data

    def test_missing_id_stays_empty_and_is_not_an_error(self):
        data = run(id_number=None)
        assert data["id_number"] is None
        assert "id_number" not in data["fields_requiring_review"]


# ---------------------------------------------------------------- 2. Dates

class TestDates:
    @pytest.mark.parametrize("raw, expected", [
        ("20/10/2026", "20/10/2026"),
        ("5/3/2026", "05/03/2026"),
        ("05.03.2026", "05/03/2026"),
        ("05-03-2026", "05/03/2026"),
        ("2026-03-05", "05/03/2026"),
        ("05/03/26", "05/03/2026"),
        ("  21/10/2026 ", "21/10/2026"),
    ])
    def test_formats_are_normalized_to_dd_mm_yyyy(self, raw, expected):
        assert v.normalize_date(raw, TODAY) == (expected, [])

    def test_today_is_not_in_the_future(self):
        assert v.normalize_date("21/10/2026", TODAY) == ("21/10/2026", [])

    @pytest.mark.parametrize("raw", ["31/02/2026", "30/02/2024", "29/02/2025", "00/10/2026", "32/01/2026"])
    def test_impossible_calendar_dates_are_invalid(self, raw):
        assert v.normalize_date(raw, TODAY) == (None, [v.DATE_INVALID])

    @pytest.mark.parametrize("raw", ["10/13/2026", "01/00/2026", "2026-13-01"])
    def test_invalid_months_are_invalid(self, raw):
        assert v.normalize_date(raw, TODAY) == (None, [v.DATE_INVALID])

    def test_leap_day_in_a_leap_year_is_valid(self):
        assert v.normalize_date("29/02/2024", TODAY) == ("29/02/2024", [])

    def test_future_date_is_flagged_but_still_shown(self):
        assert v.normalize_date("22/10/2026", TODAY) == ("22/10/2026", [v.DATE_FUTURE])

    @pytest.mark.parametrize("raw", ["yesterday", "2026", "20 באוקטובר", "20/10", "2026/10/20", "20/10/202"])
    def test_unparseable_text_is_a_format_error(self, raw):
        assert v.normalize_date(raw, TODAY) == (None, [v.DATE_BAD_FORMAT])

    def test_pipeline_rewrites_the_format_and_flags_a_future_date(self):
        data = run(received_date="2026-12-01")
        assert data["received_date"] == "01/12/2026"
        assert data["validation_errors"]["received_date"] == [v.DATE_FUTURE]
        assert "received_date" in data["fields_requiring_review"]

    def test_pipeline_keeps_an_invalid_date_text_and_flags_it(self):
        data = run(received_date="31/02/2026")
        assert data["received_date"] == "31/02/2026"  # never replaced by a guess
        assert data["validation_errors"]["received_date"] == [v.DATE_INVALID]

    def test_a_pure_reformat_is_not_flagged(self):
        data = run(received_date="5.3.2026")
        assert data["received_date"] == "05/03/2026"
        assert "received_date" not in data["fields_requiring_review"]

    def test_missing_date_stays_empty(self):
        data = run(received_date=None)
        assert data["received_date"] is None
        assert "received_date" not in data["fields_requiring_review"]


# ---------------------------------------------------------------- 3. Closed list

class TestSender:
    @pytest.mark.parametrize("name", ["כללית", "מכבי", "מאוחדת", "לאומית"])
    def test_the_four_health_funds_are_on_the_list(self, name):
        assert v.canonical_sender(name) == name

    def test_hospital_from_the_list_is_accepted(self):
        hospital = 'המרכז הרפואי ע"ש חיים שיבא - תל השומר'
        assert v.canonical_sender(hospital) == hospital

    @pytest.mark.parametrize("alias, canonical", [
        ("Clalit", "כללית"), ("maccabi", "מכבי"), ("MEUHEDET", "מאוחדת"),
        ("leumit", "לאומית"), ("  שירותי בריאות כללית ", "כללית"), ("שיבא", 'המרכז הרפואי ע"ש חיים שיבא - תל השומר'),
    ])
    def test_known_aliases_map_to_the_canonical_name(self, alias, canonical):
        assert v.canonical_sender(alias) == canonical

    @pytest.mark.parametrize("name", ["Blue Cross", "קופת חולים לא קיימת", "מכבי פלוס", "בית חולים הדמיוני", "?"])
    def test_anything_else_is_not_on_the_list(self, name):
        assert v.canonical_sender(name) is None

    def test_pipeline_canonicalizes_an_alias_without_flagging(self):
        data = run(sending_entity="clalit")
        assert data["sending_entity"] == "כללית"
        assert "sending_entity" not in data["fields_requiring_review"]

    def test_pipeline_clears_and_flags_an_unknown_sender(self):
        data = run(sending_entity="Blue Cross")
        assert data["sending_entity"] is None  # a plausible wrong name is worse than an empty one
        assert "sending_entity" in data["fields_requiring_review"]
        assert data["validation_errors"]["sending_entity"] == [v.SENDER_NOT_ON_LIST]

    def test_missing_sender_stays_empty_and_is_not_guessed(self):
        data = run(sending_entity=None)
        assert data["sending_entity"] is None


# ---------------------------------------------------------------- 4. Multiple numbers

class TestNumberLists:
    def test_several_central_numbers_are_all_kept_in_order(self):
        data = run(central_numbers=["111", "222", "333"])
        assert data["central_numbers"] == ["111", "222", "333"]

    def test_several_commitment_numbers_are_all_kept_in_order(self):
        data = run(commitment_numbers=["A-1", "דרישה 22", "333"])
        assert data["commitment_numbers"] == ["A-1", "דרישה 22", "333"]

    def test_duplicates_are_not_merged(self):
        assert run(central_numbers=["111", "111"])["central_numbers"] == ["111", "111"]

    def test_a_comma_separated_string_becomes_a_list(self):
        data = run(central_numbers="111, 222,333")
        assert data["central_numbers"] == ["111", "222", "333"]

    def test_blank_entries_are_dropped_but_real_ones_survive(self):
        assert v.normalize_number_list(["111", " ", "", "222"]) == ["111", "222"]

    def test_a_single_value_stays_a_one_item_list(self):
        assert run(commitment_numbers=["555"])["commitment_numbers"] == ["555"]

    @pytest.mark.parametrize("value", [[], None])
    def test_empty_stays_empty(self, value):
        data = run(central_numbers=value)
        assert data["central_numbers"] == value
        assert "central_numbers" not in data["fields_requiring_review"]

    def test_central_number_with_letters_is_flagged_with_all_values_kept(self):
        data = run(central_numbers=["111", "12S45"])
        assert data["central_numbers"] == ["111", "12S45"]  # not "fixed" to 12545
        assert data["validation_errors"]["central_numbers"] == [v.NUMBER_NOT_DIGITS]
        assert "central_numbers" in data["fields_requiring_review"]

    def test_commitment_numbers_may_carry_other_characters(self):
        assert "commitment_numbers" not in run(commitment_numbers=["AB-12/3"])["fields_requiring_review"]

    def test_a_malformed_list_is_flagged(self):
        data = run(central_numbers=[1, 2])
        assert data["validation_errors"]["central_numbers"] == [v.NUMBER_LIST_MALFORMED]


# ---------------------------------------------------------------- 5. Confidence and review flags

class TestFlagsAndConfidence:
    def test_a_failing_field_cannot_stay_at_certain_confidence(self):
        data = run(id_number="11223344")
        assert data["confidence"]["id_number"] == OK_ABOVE
        assert data["confidence"]["first_name"] == 100  # untouched fields keep their score

    def test_a_lower_score_is_not_raised(self):
        data = extraction(id_number="11223344")
        data["confidence"]["id_number"] = 60
        v.validate_extraction(data, today=TODAY, ok_above=OK_ABOVE)
        assert data["confidence"]["id_number"] == 60

    def test_every_failure_is_flagged_once_and_a_model_flag_is_not_duplicated(self):
        data = extraction(id_number="11223344", sending_entity="Blue Cross", received_date="31/02/2026")
        data["fields_requiring_review"] = ["id_number"]
        v.validate_extraction(data, today=TODAY, ok_above=OK_ABOVE)
        assert sorted(data["fields_requiring_review"]) == ["id_number", "received_date", "sending_entity"]

    def test_a_missing_review_list_is_recreated(self):
        data = extraction(id_number="11223344")
        del data["fields_requiring_review"]
        v.validate_extraction(data, today=TODAY, ok_above=OK_ABOVE)
        assert data["fields_requiring_review"] == ["id_number"]

    def test_a_fully_valid_form_raises_no_flags(self):
        data = run()
        assert data["fields_requiring_review"] == []
        assert "validation_errors" not in data

    def test_names_are_never_modified(self):
        data = run(first_name="  דנה ", last_name="Lévy")
        assert (data["first_name"], data["last_name"]) == ("  דנה ", "Lévy")

    def test_the_review_label_is_the_one_the_lab_asked_for(self):
        assert v.REVIEW_LABEL == "לא בטוח - בדקי"

    def test_everything_wrong_at_once(self):
        data = run(id_number="1", received_date="99/99/9999", sending_entity="X", central_numbers=["a"])
        assert set(data["validation_errors"]) == {"id_number", "received_date", "sending_entity", "central_numbers"}
        assert all(data["confidence"][f] <= OK_ABOVE for f in data["validation_errors"])
