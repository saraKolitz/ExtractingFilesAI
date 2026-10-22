"""Tests for backend/app/services/orientation.py. The model call is replaced by a stub."""

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from backend.app.services import orientation as o


def png(image: np.ndarray) -> bytes:
    return cv2.imencode(".png", image)[1].tobytes()


def decode(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def page_with_marker() -> tuple[np.ndarray, list[float]]:
    """A 300x200 (WxH) black page with a white marker, and the marker's box in page fractions."""
    page = np.zeros((200, 300, 3), np.uint8)
    page[20:60, 210:270] = 255  # y 20-60, x 210-270
    return page, [210 / 300, 20 / 200, 270 / 300, 60 / 200]


def marker_box(image: np.ndarray) -> list[float]:
    ys, xs = np.where(image[:, :, 0] == 255)
    h, w = image.shape[:2]
    return [xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h]


class TestRotate:
    @pytest.mark.parametrize("degrees, shape", [(0, (200, 300)), (90, (300, 200)), (180, (200, 300)), (270, (300, 200))])
    def test_dimensions_follow_the_rotation(self, degrees, shape):
        page, _ = page_with_marker()
        assert decode(o.rotate_image(png(page), degrees)).shape[:2] == shape

    def test_zero_returns_the_same_bytes(self):
        data = png(page_with_marker()[0])
        assert o.rotate_image(data, 0) is data

    def test_90_is_clockwise(self):
        page = np.zeros((100, 200, 3), np.uint8)
        page[0:10, 0:10] = 255  # top-left corner
        turned = decode(o.rotate_image(png(page), 90))
        assert turned[0:10, -10:].min() == 255  # a clockwise turn moves top-left to top-right

    def test_garbage_bytes_raise(self):
        with pytest.raises(ValueError):
            o.rotate_image(b"nope", 90)


class TestUnrotateBox:
    @pytest.mark.parametrize("degrees", [0, 90, 180, 270])
    def test_a_box_found_on_the_rotated_copy_maps_back_to_the_original(self, degrees):
        page, original_box = page_with_marker()
        rotated = decode(o.rotate_image(png(page), degrees))
        found = o.unrotate_box(marker_box(rotated), degrees)
        assert found == pytest.approx(original_box, abs=1e-3)

    def test_the_result_is_ordered(self):
        x0, y0, x1, y1 = o.unrotate_box([0.1, 0.2, 0.3, 0.4], 270)
        assert x0 <= x1 and y0 <= y1


class TestParseRotation:
    @pytest.mark.parametrize("text, expected", [
        ('{"rotate_clockwise": 270}', 270),
        ('```json\n{"rotate_clockwise": 90}\n```', 90),
        ('{"rotate_clockwise": 0}', 0),
        ('{"rotate_clockwise": 45}', 0),
        ('{"rotate_clockwise": "90"}', 0),
        ('{"rotate_clockwise": true}', 0),
        ('{"other": 90}', 0),
        ("rotate it by 90", 0),
        ("", 0),
    ])
    def test_only_a_valid_answer_rotates(self, text, expected):
        assert o.parse_rotation(text) == expected


class TestDetect:
    def stub(self, text, stop_reason="end_turn"):
        block = SimpleNamespace(type="text", text=text)
        response = SimpleNamespace(stop_reason=stop_reason, content=[block])
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return response

        return SimpleNamespace(messages=SimpleNamespace(create=create)), calls

    def test_returns_the_models_answer_and_sends_a_downscaled_image(self):
        client, calls = self.stub('{"rotate_clockwise": 270}')
        big = png(np.zeros((3000, 2000, 3), np.uint8))
        assert o.detect_rotation(client, "m", big) == 270
        assert len(calls) == 1 and calls[0]["model"] == "m"
        sent = calls[0]["messages"][0]["content"][0]["source"]["data"]
        import base64
        assert max(decode(base64.b64decode(sent)).shape[:2]) == o.DETECT_MAX_SIDE

    @pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
    def test_a_refusal_or_cut_off_reply_leaves_the_page_alone(self, stop_reason):
        client, _ = self.stub('{"rotate_clockwise": 90}', stop_reason)
        assert o.detect_rotation(client, "m", png(np.zeros((50, 50, 3), np.uint8))) == 0


# ---------------------------------------------------------------- Tesseract OSD output

def osd(rotate, confidence):
    return chr(10).join([
        "Page number: 0", "Orientation in degrees: 90", f"Rotate: {rotate}",
        f"Orientation confidence: {confidence}", "Script: Hebrew",
    ])


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_osd_rotation_is_read(rotate):
    from backend.app.services.orientation import parse_osd
    assert parse_osd(osd(rotate, "12.3")) == rotate


def test_osd_below_the_confidence_floor_is_unknown():
    from backend.app.services.orientation import parse_osd
    assert parse_osd(osd(270, "0.4")) is None


@pytest.mark.parametrize("text", ["", "Too few characters. Skipping this page", osd(45, 9)])
def test_unusable_osd_output_is_unknown(text):
    from backend.app.services.orientation import parse_osd
    assert parse_osd(text) is None
