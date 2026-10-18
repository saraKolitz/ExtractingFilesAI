import os
from pathlib import Path

from google.cloud import vision
from google.oauth2 import service_account

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _vision_client() -> vision.ImageAnnotatorClient:
    """Vision client with the service account from OCR_CREDENTIALS_FILE in .env.

    A relative path is taken from backend/, so it works whatever folder the server starts in.
    """
    creds_path = os.getenv("OCR_CREDENTIALS_FILE")
    if not creds_path:
        raise RuntimeError("OCR_CREDENTIALS_FILE is not set in .env")
    credentials = service_account.Credentials.from_service_account_file(BACKEND_DIR / creds_path)
    return vision.ImageAnnotatorClient(credentials=credentials)


def extract_text_from_image(image_content: bytes) -> str:
    """Run Google Cloud Vision document text detection on an image and return the full text."""
    client = _vision_client()
    image = vision.Image(content=image_content)
    # The forms are printed Hebrew, with handwritten Hebrew names and ID numbers.
    image_context = vision.ImageContext(language_hints=["iw", "he"])
    response = client.document_text_detection(image=image, image_context=image_context)
    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")
    return response.full_text_annotation.text


def get_word_coordinates(image_bytes: bytes) -> list[dict]:
    """Run document text detection and return each detected word with its bounding polygon.

    Format: [{"word": str, "vertices": [{"x": int, "y": int}, ...]}, ...]
    """
    client = _vision_client()
    image = vision.Image(content=image_bytes)
    image_context = vision.ImageContext(language_hints=["iw", "he"])
    response = client.document_text_detection(image=image, image_context=image_context)
    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")
    # text_annotations[0] is the whole page's text; the rest are single words.
    return [
        {
            "word": annotation.description,
            "vertices": [{"x": v.x, "y": v.y} for v in annotation.bounding_poly.vertices],
        }
        for annotation in response.text_annotations[1:]
    ]
