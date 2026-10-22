"""Barcodes on the page as a second source for the central number (CLAUDE.md step 8, section 6).

The stickers carry a barcode with the same number that is printed under it, so the decoded value is
an independent reading of the number the model read from the picture. It is used only to CONFIRM
or to point at a likely misreading - a barcode is never copied into a field, because nothing says
which sticker on the page is the one labelled "מרכזי".
"""

import re

import cv2
import numpy as np
import zxingcpp

CONFIRMABLE_FIELD = "central_numbers"
# A model reading that differs from a barcode in at most this many digits is a probable misread.
MAX_NEAR_MISS = 2


def decode_barcodes(image_bytes: bytes) -> list[str]:
    """Every barcode value found on the page, in reading order of the decoder, without repeats.

    zxing tries rotated barcodes too, which matters: scans of these forms are often sideways.
    """
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return []
    seen: list[str] = []
    for result in zxingcpp.read_barcodes(image):
        text = result.text.strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _digits(value: str) -> str:
    # The printed number can carry a letter prefix ("X132797"); the digits are what is compared.
    return re.sub(r"\D", "", value)


def _near(a: str, b: str) -> bool:
    return a != b and len(a) == len(b) and sum(x != y for x, y in zip(a, b)) <= MAX_NEAR_MISS


def apply_barcode_check(data: dict, codes: list[str]) -> None:
    """Cross-checks the central numbers against the barcodes, in place.

    - Every central number matches a barcode: the field is confirmed by a second source - its
      confidence goes to 100 and, unless a hard validity check failed, its review flag is dropped.
    - A number that is one or two digits away from a barcode is a probable misreading: the field
      is flagged, and the pair is recorded under data["barcode_suggestions"] for the reviewer.
    - Anything else changes nothing: a barcode that matches no number may belong to another sticker.
    data["barcodes"] always lists what was decoded, as hints for the reviewer.
    """
    data["barcodes"] = codes
    numbers = data.get(CONFIRMABLE_FIELD)
    if not codes or not isinstance(numbers, list) or not numbers:
        return
    code_digits = {_digits(c): c for c in codes if _digits(c)}
    read = [(n, _digits(n)) for n in numbers if isinstance(n, str)]
    if not read or any(not d for _, d in read):
        return

    suggestions = [
        {"read": n, "barcode": code_digits[b]}
        for n, d in read
        if d not in code_digits
        for b in code_digits
        if _near(d, b)
    ]
    if suggestions:
        data["barcode_suggestions"] = {CONFIRMABLE_FIELD: suggestions}
        flags = data.setdefault("fields_requiring_review", [])
        if CONFIRMABLE_FIELD not in flags:
            flags.append(CONFIRMABLE_FIELD)
        return

    failed_a_hard_check = CONFIRMABLE_FIELD in (data.get("validation_errors") or {})
    if all(d in code_digits for _, d in read) and not failed_a_hard_check:
        data["barcode_confirmed"] = [CONFIRMABLE_FIELD]
        confidence = data.get("confidence")
        if isinstance(confidence, dict):
            confidence[CONFIRMABLE_FIELD] = 100
        flags = data.get("fields_requiring_review")
        if isinstance(flags, list) and CONFIRMABLE_FIELD in flags:
            flags.remove(CONFIRMABLE_FIELD)
