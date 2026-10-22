"""Hard validity checks on the extracted fields (CLAUDE.md sections 2-E and 5).

Pure functions, no network and no model: the model's own confidence is only an estimate, so these
rules run in code afterwards. A check never "repairs" a value from what looks reasonable (section
6) - it either accepts it (a date may be re-formatted, a sender alias mapped to its canonical
name), or it reports an error and the field goes to the reviewer. An empty field is never an error.
"""

import json
import re
from datetime import date
from pathlib import Path

ENTITY_TEMPLATES_PATH = Path(__file__).resolve().parent / "entity_templates.json"
with open(ENTITY_TEMPLATES_PATH, encoding="utf-8") as _f:
    _ENTITY_TEMPLATES: dict = json.load(_f)

# Closed list of senders (data model field 6): canonical name, and every known spelling of it.
ALIAS_TO_ENTITY = {
    alias.casefold(): name
    for name, template in _ENTITY_TEMPLATES.items()
    for alias in [name, *template.get("aliases", [])]
}

REVIEW_LABEL = "לא בטוח - בדקי"
ID_LENGTH = 9

# Error codes, one per rule, so the tests and the UI can tell the failures apart.
ID_NOT_DIGITS = "id_not_digits"
ID_WRONG_LENGTH = "id_wrong_length"
ID_BAD_CHECKSUM = "id_bad_checksum"
DATE_BAD_FORMAT = "date_bad_format"
DATE_INVALID = "date_invalid"
DATE_FUTURE = "date_future"
SENDER_NOT_ON_LIST = "sender_not_on_list"
NUMBER_NOT_DIGITS = "number_not_digits"
NUMBER_LIST_MALFORMED = "number_list_malformed"

_DMY = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def is_empty(value) -> bool:
    return value is None or (isinstance(value, (str, list)) and len(value) == 0)


def validate_id_number(value: str) -> list[str]:
    """Exactly 9 digits and a valid Israeli ID check digit.

    No padding: an 8-digit ID is reported, not completed with a zero (CLAUDE.md decision 2).
    """
    if not value.isascii() or not value.isdigit():
        return [ID_NOT_DIGITS]
    if len(value) != ID_LENGTH:
        return [ID_WRONG_LENGTH]
    # Weights 1,2,1,2...; a two-digit product is reduced to the sum of its digits.
    total = 0
    for i, ch in enumerate(value):
        n = int(ch) * (1 if i % 2 == 0 else 2)
        total += n - 9 if n > 9 else n
    return [] if total % 10 == 0 else [ID_BAD_CHECKSUM]


def id_blocks_saving(value: str) -> bool:
    """True if a non-empty ID number may not be saved or exported: not exactly 9 digits.

    A wrong check digit is only flagged for review, not blocked - the lab can still have to
    record an ID that was printed or typed wrongly on the form.
    """
    value = value.strip()
    return bool(value) and bool({ID_NOT_DIGITS, ID_WRONG_LENGTH} & set(validate_id_number(value)))


def normalize_date(value: str, today: date | None = None) -> tuple[str | None, list[str]]:
    """Returns (DD/MM/YYYY, errors). The text is read day-first, as on Israeli forms.

    Accepts DD/MM/YYYY with "/", "." or "-", a two-digit year (read as 20YY) and ISO YYYY-MM-DD.
    Anything else, an impossible calendar date (31/02) or a date after today is an error; the
    normalized text is still returned for a future date so the reviewer sees what was read.
    """
    today = today or date.today()
    text = value.strip()
    if m := _ISO.fullmatch(text):
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    elif m := _DMY.fullmatch(text):
        day, month, year = int(m[1]), int(m[2]), int(m[3])
        if len(m[3]) == 2:
            year += 2000
    else:
        return None, [DATE_BAD_FORMAT]
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None, [DATE_INVALID]
    normalized = parsed.strftime("%d/%m/%Y")
    return normalized, [DATE_FUTURE] if parsed > today else []


def canonical_sender(value: str) -> str | None:
    """The closed-list name for a sender (or a known alias of it); None if it is not on the list."""
    return ALIAS_TO_ENTITY.get(value.strip().casefold())


def normalize_number_list(value) -> list[str] | None:
    """A multi-value field as a list of strings, in order, nothing dropped or merged.

    A model that returns "123, 456" as one string is split on commas; None means the value is
    neither a string nor a list of strings.
    """
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return [v.strip() for v in value if v.strip()]


def validate_extraction(data: dict, today: date | None = None, ok_above: int = 90) -> None:
    """Runs every rule over the extraction result, in place.

    A failing field is added to fields_requiring_review, its codes are recorded under
    data["validation_errors"][field], and its confidence is capped at ok_above so a field that
    breaks a hard rule can never read as certain. Values are normalized (date format, list shape,
    sender spelling) but never corrected; an off-list sender is cleared, as the closed list requires.
    """
    errors: dict[str, list[str]] = {}

    def fail(field: str, codes: list[str]) -> None:
        errors[field] = codes
        flags = data.get("fields_requiring_review")
        if not isinstance(flags, list):
            flags = data["fields_requiring_review"] = []
        if field not in flags:
            flags.append(field)
        confidence = data.get("confidence")
        if isinstance(confidence, dict) and isinstance(confidence.get(field), (int, float)):
            confidence[field] = min(confidence[field], ok_above)

    id_number = data.get("id_number")
    if isinstance(id_number, str) and id_number.strip():
        data["id_number"] = id_number = id_number.strip()
        if codes := validate_id_number(id_number):
            fail("id_number", codes)

    received = data.get("received_date")
    if isinstance(received, str) and received.strip():
        normalized, codes = normalize_date(received, today)
        if normalized:
            data["received_date"] = normalized
        if codes:
            fail("received_date", codes)

    sender = data.get("sending_entity")
    if isinstance(sender, str) and sender.strip():
        canonical = canonical_sender(sender)
        data["sending_entity"] = canonical
        if canonical is None:
            fail("sending_entity", [SENDER_NOT_ON_LIST])

    for field in ("central_numbers", "commitment_numbers"):
        value = data.get(field)
        if is_empty(value):
            continue
        numbers = normalize_number_list(value)
        if numbers is None:
            fail(field, [NUMBER_LIST_MALFORMED])
            continue
        data[field] = numbers
        # Central numbers are digit-only (section 6); commitment numbers may carry other labels.
        if field == "central_numbers" and not all(n.isascii() and n.isdigit() for n in numbers):
            fail(field, [NUMBER_NOT_DIGITS])

    if errors:
        data["validation_errors"] = errors
