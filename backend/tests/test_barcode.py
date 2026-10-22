"""Tests for backend/app/services/barcode.py. Barcodes are generated here, not taken from samples."""

import cv2
import numpy as np
import pytest
import zxingcpp

from backend.app.services import barcode as b


def png_of(text: str, fmt=zxingcpp.BarcodeFormat.Code128, rotate=None) -> bytes:
    image = np.array(zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, fmt), scale=3))
    image = cv2.copyMakeBorder(image, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    if rotate is not None:
        image = cv2.rotate(image, rotate)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def result(**fields) -> dict:
    data = {
        "central_numbers": ["26224944"],
        "confidence": {"central_numbers": 70},
        "fields_requiring_review": ["central_numbers"],
    }
    data.update(fields)
    return data


class TestDecode:
    def test_reads_a_code128_barcode(self):
        assert b.decode_barcodes(png_of("26224944")) == ["26224944"]

    def test_reads_a_sideways_barcode(self):
        assert b.decode_barcodes(png_of("948246", rotate=cv2.ROTATE_90_CLOCKWISE)) == ["948246"]

    def test_reads_code39(self):
        assert b.decode_barcodes(png_of("132797", fmt=zxingcpp.BarcodeFormat.Code39)) == ["132797"]

    def test_a_page_without_a_barcode_gives_an_empty_list(self):
        blank = cv2.imencode(".png", np.full((300, 300, 3), 255, np.uint8))[1].tobytes()
        assert b.decode_barcodes(blank) == []

    def test_garbage_bytes_give_an_empty_list(self):
        assert b.decode_barcodes(b"not an image") == []


class TestCheck:
    def test_a_matching_barcode_confirms_the_field(self):
        data = result()
        b.apply_barcode_check(data, ["26224944"])
        assert data["barcode_confirmed"] == ["central_numbers"]
        assert data["confidence"]["central_numbers"] == 100
        assert "central_numbers" not in data["fields_requiring_review"]

    def test_a_letter_prefix_on_either_side_is_ignored(self):
        data = result(central_numbers=["132797"])
        b.apply_barcode_check(data, ["X132797"])
        assert data["barcode_confirmed"] == ["central_numbers"]

    def test_all_numbers_must_match_to_confirm(self):
        data = result(central_numbers=["26224944", "999999"])
        b.apply_barcode_check(data, ["26224944"])
        assert "barcode_confirmed" not in data
        assert "central_numbers" in data["fields_requiring_review"]
        assert data["confidence"]["central_numbers"] == 70

    def test_a_field_that_failed_a_hard_check_is_not_confirmed(self):
        data = result(validation_errors={"central_numbers": ["number_not_digits"]})
        b.apply_barcode_check(data, ["26224944"])
        assert "barcode_confirmed" not in data
        assert "central_numbers" in data["fields_requiring_review"]

    def test_a_near_miss_is_flagged_with_the_barcode_value_but_not_replaced(self):
        data = result(central_numbers=["26224948"], fields_requiring_review=[])
        b.apply_barcode_check(data, ["26224944"])
        assert data["central_numbers"] == ["26224948"]  # never "fixed" automatically
        assert data["barcode_suggestions"] == {"central_numbers": [{"read": "26224948", "barcode": "26224944"}]}
        assert data["fields_requiring_review"] == ["central_numbers"]
        assert "barcode_confirmed" not in data

    def test_an_unrelated_barcode_changes_nothing(self):
        data = result()
        b.apply_barcode_check(data, ["948246"])
        assert data["confidence"]["central_numbers"] == 70
        assert data["fields_requiring_review"] == ["central_numbers"]
        assert "barcode_suggestions" not in data and "barcode_confirmed" not in data

    @pytest.mark.parametrize("numbers", [[], None])
    def test_an_empty_field_is_never_filled_from_a_barcode(self, numbers):
        data = result(central_numbers=numbers)
        b.apply_barcode_check(data, ["26224944"])
        assert data["central_numbers"] == numbers
        assert data["barcodes"] == ["26224944"]  # offered to the reviewer as a hint only

    def test_no_barcodes_means_no_change(self):
        data = result()
        b.apply_barcode_check(data, [])
        assert data["confidence"]["central_numbers"] == 70
        assert data["barcodes"] == []
