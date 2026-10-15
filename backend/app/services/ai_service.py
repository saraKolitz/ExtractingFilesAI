import logging
import os
from pathlib import Path

import google.generativeai as genai
from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
# The API is configured at import time, before main.py's load_dotenv() call can run.
load_dotenv(ROOT / ".env")

# Logs must never contain personal data (file names, content) - only error types.
logger = logging.getLogger("uvicorn.error")

DEFAULT_MODEL = "gemini-1.5-flash"
# extract_data_from_image returns its errors as text starting with this prefix.
ERROR_PREFIX = "Error: "
PROMPT = """You are an expert AI data extraction system specialized in scanning Hebrew administrative forms.
Your task is to extract specific data fields from the provided image and return ONLY a valid, structured JSON object. Do not include any Markdown formatting, explanations, or conversational text outside the JSON.

### EXTRACTION RULES:
1. STRICT ACCURACY: Do NOT guess or invent information. If a field is not present or cannot be read, return `null` for strings or an empty array `[]` for lists.
2. UNCERTAINTY: If you extract a value but are not 100% confident in your reading (e.g., due to unclear handwriting), add the exact JSON key of that field to the `fields_requiring_review` array.
3. MULTIPLE VALUES: Fields defined as arrays may contain multiple numbers. Extract and save all of them.

### FIELDS TO EXTRACT:
- "first_name" (String): The private name of the patient.
- "last_name" (String): The family name of the patient.
- "id_number" (String): The ID number (תעודת זהות).
- "central_numbers" (Array of Strings): מספר מרכזי. There may be multiple. Often found on a sticker, on the side, or handwritten.
- "received_date" (String): תאריך קבלת הדגימה. Look for a stamp saying "התקבל" or a handwritten date, often located under the central number.
- "sending_entity" (String): גורם שולח / קופת חולים / בית חולים.
- "commitment_numbers" (Array of Strings): מספר התחייבות. There may be multiple. Also look for the terms "דרישה", or "דגימה" (specifically for Leumit).

### JSON OUTPUT FORMAT:
{
  "first_name": null,
  "last_name": null,
  "id_number": null,
  "central_numbers": [],
  "received_date": null,
  "sending_entity": null,
  "commitment_numbers": [],
  "fields_requiring_review": []
}
"""

# REST and not the default gRPC: when the connection fails (e.g. a TLS error) gRPC retries for
# minutes, so the except below would never get to return the error.
genai.configure(api_key=os.getenv("API_KEY") or None, transport="rest")


def extract_data_from_image(image_path: str) -> str:
    try:
        # "or" and not a getenv default: an empty MODEL_NAME= line in .env must fall back too.
        model = genai.GenerativeModel(os.getenv("MODEL_NAME") or DEFAULT_MODEL)
        with Image.open(image_path) as image:
            response = model.generate_content([PROMPT, image])
        return response.text
    except Exception as exc:
        logger.error("image extraction failed: %s", type(exc).__name__)
        return f"{ERROR_PREFIX}{exc}"
