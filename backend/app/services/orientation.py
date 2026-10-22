"""Detects a sideways or upside-down scan and rotates a working copy for the extraction.

Many scans of these forms come out rotated by 90 degrees, and the model reads fewer fields from a
rotated page (the ID number on one sample page was lost until the page was turned upright). The
stored page image is never touched: only the copy sent to Claude is rotated, and the field regions
the extraction records are mapped back to the original page, which is what the screen shows.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

VALID_ROTATIONS = (0, 90, 180, 270)
# Long side of the picture used for the detection; the orientation is obvious at this size and a
# smaller image costs fewer tokens.
DETECT_MAX_SIDE = 1000

ORIENTATION_PROMPT = """This is a scan of a Hebrew medical form. Scans are sometimes sideways or
upside down. Decide how many degrees the picture must be rotated CLOCKWISE so that the printed
text lines run horizontally and read upright, with the top of the form at the top.

Answer with exactly one JSON object and nothing else: {"rotate_clockwise": 0}
The value is one of 0, 90, 180 or 270. Ignore a slight tilt of a few degrees. If the page is
already upright, or you cannot tell, answer 0."""


def rotate_image(image_bytes: bytes, degrees: int) -> bytes:
    """The picture turned clockwise by 0/90/180/270 degrees, as PNG bytes."""
    if degrees == 0:
        return image_bytes
    codes = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode image")
    ok, encoded = cv2.imencode(".png", cv2.rotate(image, codes[degrees]))
    if not ok:
        raise ValueError("could not encode rotated image")
    return encoded.tobytes()


def unrotate_box(box: list[float], degrees: int) -> list[float]:
    """Maps [x0, y0, x1, y1] page fractions from the rotated copy back to the original page."""
    x0, y0, x1, y1 = box
    if degrees == 0:
        return box

    def back(u: float, v: float) -> tuple[float, float]:
        if degrees == 90:
            return v, 1 - u
        if degrees == 270:
            return 1 - v, u
        return 1 - u, 1 - v

    ax, ay = back(x0, y0)
    bx, by = back(x1, y1)
    return [round(min(ax, bx), 4), round(min(ay, by), 4), round(max(ax, bx), 4), round(max(ay, by), 4)]


def parse_rotation(text: str) -> int:
    """The rotation in the model's reply; 0 if the reply is not what was asked for."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return 0
    try:
        value = json.loads(match.group(0)).get("rotate_clockwise")
    except (ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value in VALID_ROTATIONS else 0


def _downscaled_png(image_bytes: bytes) -> bytes:
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode image")
    longest = max(image.shape[:2])
    if longest > DETECT_MAX_SIDE:
        scale = DETECT_MAX_SIDE / longest
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("could not encode image")
    return encoded.tobytes()


# Tesseract's own orientation confidence; under this the answer is not trusted. On the 36 sample
# pages every answer agreed with the model's, including the lowest at 1.6.
TESSERACT_MIN_CONFIDENCE = 1.0
_TESSERACT_DEFAULT = "C:/Program Files/Tesseract-OCR/tesseract.exe"


def _tesseract_path() -> str | None:
    """The tesseract executable: TESSERACT_CMD in .env, else the PATH, else the default install."""
    configured = os.getenv("TESSERACT_CMD")
    for candidate in (configured, shutil.which("tesseract"), _TESSERACT_DEFAULT):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def parse_osd(text: str) -> int | None:
    """The clockwise rotation from `tesseract --psm 0` output; None if absent or not confident."""
    rotate = re.search(r"Rotate:\s*(\d+)", text)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", text)
    if not rotate or not confidence:
        return None
    degrees = int(rotate.group(1))
    if degrees not in VALID_ROTATIONS or float(confidence.group(1)) < TESSERACT_MIN_CONFIDENCE:
        return None
    return degrees


def tesseract_rotation(image_bytes: bytes) -> int | None:
    """Orientation by Tesseract's OSD: free, local, about a second a page. None means "unknown"
    (not installed, failed, or not confident), and the caller falls back to the model."""
    exe = _tesseract_path()
    if exe is None:
        return None
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "page.png")
        cv2.imwrite(path, image)
        try:
            done = subprocess.run(
                [exe, path, "stdout", "--psm", "0", "-l", "osd"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    return parse_osd(done.stdout)


def detect_rotation(client, model: str, image_bytes: bytes) -> int:
    """Asks Claude how far to turn the page clockwise to make it upright (0, 90, 180 or 270).

    Takes the Anthropic client and model name as arguments so this module does not import the
    extraction code. Any reply that is not a valid answer counts as "leave it as it is".
    """
    data = base64.standard_b64encode(_downscaled_png(image_bytes)).decode("utf-8")
    response = client.messages.create(
        model=model,
        # Thinking tokens count toward max_tokens; the answer itself is a dozen characters.
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                {"type": "text", "text": ORIENTATION_PROMPT},
            ],
        }],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        return 0
    return parse_rotation("".join(b.text for b in response.content if b.type == "text"))
