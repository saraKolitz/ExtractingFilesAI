import io
import json
import logging
import os
from pathlib import Path

import google.generativeai as genai
from dotenv import load_dotenv
from PIL import Image

BACKEND_DIR = Path(__file__).resolve().parents[2]
# The API is configured at import time, before main.py's load_dotenv() call can run.
load_dotenv(BACKEND_DIR / ".env")

# Logs must never contain personal data (file names, content) - only error types.
logger = logging.getLogger("uvicorn.error")

DEFAULT_MODEL = "gemini-1.5-flash"
# extract_data_from_image returns its errors as text starting with this prefix.
ERROR_PREFIX = "Error: "

# Sending entities (closed list, see data model field 6) each have a fixed form layout.
# This maps entity name -> {aliases: [...], hints: "..."}. aliases document known name variants
# for each entity (used to build the classification prompt below); hints are entity-specific
# extraction notes (field locations, label wording, etc.) filled in as real samples for that
# entity are reviewed, instead of guessed up front.
ENTITY_TEMPLATES_PATH = Path(__file__).resolve().parent / "entity_templates.json"
with open(ENTITY_TEMPLATES_PATH, encoding="utf-8") as _f:
    ENTITY_TEMPLATES: dict = json.load(_f)

# Single classification pass, done in one call: try the visual logo/letterhead first (survives
# noisy OCR and handwriting), and only fall back to the entity's name elsewhere in the page's
# text if no logo can be matched confidently - both attempts happen inside this one prompt.
ENTITY_CLASSIFICATION_PROMPT = """Look at this image of a Hebrew medical form, and identify which
institution (hospital or health fund / קופת חולים) issued it.

Closed list of possible values, copied exactly as written: <<ENTITY_LIST>>

Follow these steps, in order:
1. First, look for a visual logo, letterhead graphic, or stamp at the top of the page. If you can
   confidently recognize the issuing institution from it, use that.
2. If there is no visible logo, or you cannot identify the institution from it alone, then look
   for the institution's name written anywhere in the text on the page instead (letterhead text,
   stamps, codes, etc.) and identify it that way.
3. If you still cannot confidently determine the issuing institution either way, respond with
   exactly the single word: NONE

Respond with ONLY the matched entity name (copied exactly from the list), or the word NONE.
No explanation, no punctuation, nothing else.
"""
ENTITY_CLASSIFICATION_PROMPT = ENTITY_CLASSIFICATION_PROMPT.replace(
    "<<ENTITY_LIST>>", ", ".join(ENTITY_TEMPLATES.keys())
)
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
- "central_numbers" (Array of Strings): מספר מרכזי. STRICT LABEL MATCH: copy a number into this
  array ONLY if it is explicitly tied to the label "מרכזי" (central) - whether printed on a sticker
  or handwritten next to/under that label. A number on a sticker, or anywhere else on the page,
  that is NOT labeled "מרכזי" must NOT be copied here, no matter how similar it looks to a central
  number. NOT ALWAYS A BARCODE: it is usually a sticker with a barcode, but on handwritten or
  screen-capture style pages it can be a handwritten number with no barcode at all - do not skip it
  just because there is no barcode. FULL PAGE SCAN: do not stop after finding the first one - scan
  the entire page for every instance labeled "מרכזי", and return ALL of them in this array (there
  is often more than one for the same sample).
- "received_date" (String): תאריך קבלת הדגימה. Look for a stamp saying "התקבל" or a handwritten date, often located under the central number.
- "sending_entity" (String): גורם שולח / קופת חולים / בית חולים. Must be copied exactly as one of
  the values in this closed list - never guess or invent a value outside it: <<ENTITY_LIST>>.
  If the form clearly names an entity that is not in this list, leave this field null and add
  "sending_entity" to `fields_requiring_review` instead of forcing the closest match.
- "commitment_numbers" (Array of Strings): מספר התחייבות. There may be multiple. The commitment
  number is not always labeled "התחייבות" - depending on the sending institution it may instead be
  labeled "דרישה" (request) or "דגימה" (sample number); on some forms the sample number is
  explicitly stated to double as the commitment number. Treat these labels as equally valid sources
  for this field, not just for one specific entity.

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
# ENTITY_TEMPLATES is the single source of truth for the closed list of sending entities;
# the prompt is built from it so the list can't drift out of sync with entity_templates.json.
PROMPT = PROMPT.replace("<<ENTITY_LIST>>", ", ".join(ENTITY_TEMPLATES.keys()))

# REST and not the default gRPC: when the connection fails (e.g. a TLS error) gRPC retries for
# minutes, so the except below would never get to return the error.
genai.configure(api_key=os.getenv("GOOGLE_API_KEY") or None, transport="rest")


def classify_entity(model, image) -> str | None:
    """Asks the model to identify the issuing entity in one pass: visual logo/letterhead first,
    falling back to the entity's name elsewhere in the page's text (see
    ENTITY_CLASSIFICATION_PROMPT). Returns the entity name if it's an exact match for one of our
    closed-list keys, otherwise None (covers both an explicit NONE reply and any unexpected text)."""
    reply = model.generate_content([ENTITY_CLASSIFICATION_PROMPT, image]).text.strip()
    return reply if reply in ENTITY_TEMPLATES else None


def extract_data_from_image(image_path: str) -> str:
    try:
        # "or" and not a getenv default: an empty MODEL_NAME= line in .env must fall back too.
        model = genai.GenerativeModel(os.getenv("MODEL_NAME") or DEFAULT_MODEL)
        with Image.open(image_path) as image:
            entity = classify_entity(model, image)

            prompt = PROMPT
            if entity:
                prompt += (
                    "\n\n### AUTOMATIC HINT (not verified, may be wrong):\n"
                    f"Automatic classification suggests the sending entity may be: {entity}.\n"
                    "Use this only as a starting point. Confirm sending_entity yourself from what "
                    "is actually printed on the page, and ignore this hint if it conflicts with "
                    "what you see.\n"
                )
                hints = ENTITY_TEMPLATES.get(entity, {}).get("hints", "")
                if hints:
                    prompt += (
                        f"Known layout notes for {entity} (also just a hint, verify against the "
                        f"image):\n{hints}\n"
                    )

            response = model.generate_content([prompt, image])
        return response.text
    except Exception as exc:
        logger.error("image extraction failed: %s", type(exc).__name__)
        return f"{ERROR_PREFIX}{exc}"


FIELD_PROMPT = (
    "This is a cropped image of the field '{field_name}' from a Hebrew form. It may be handwritten, "
    "printed, or empty. Read the text. If it is empty or illegible, return 'null'. Return ONLY the "
    "extracted value without any markdown, explanations, or extra text."
)


def extract_fields_with_gemini(cropped_images: dict[str, bytes]) -> dict:
    """Read each cropped field image with Gemini. Returns {field_name: text, or None if empty}.

    API errors are raised, not swallowed: a failed call must not look like an empty field.
    """
    model = genai.GenerativeModel(os.getenv("MODEL_NAME") or DEFAULT_MODEL)
    results: dict[str, str | None] = {}
    for field_name, image_bytes in cropped_images.items():
        with Image.open(io.BytesIO(image_bytes)) as image:
            response = model.generate_content([FIELD_PROMPT.format(field_name=field_name), image])
        text = response.text.strip().strip("'\"`").strip()
        # A missing field stays empty - never a guessed value or the literal word "null".
        results[field_name] = None if not text or text.lower() == "null" else text
    return results
