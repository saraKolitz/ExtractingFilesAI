import base64
# import io
import json
import logging
import os
from pathlib import Path

import anthropic
import cv2
import numpy as np
# import google.generativeai as genai
from dotenv import load_dotenv
# from PIL import Image

BACKEND_DIR = Path(__file__).resolve().parents[2]
# The API is configured at import time, before main.py's load_dotenv() call can run.
load_dotenv(BACKEND_DIR / ".env")

# Logs must never contain personal data (file names, content) - only error types.
logger = logging.getLogger("uvicorn.error")

# DEFAULT_MODEL = "gemini-1.5-flash"
# # extract_data_from_image returns its errors as text starting with this prefix.
# ERROR_PREFIX = "Error: "

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
# PROMPT += """
# ### AGENT LOOP & DYNAMIC CROPPING INSTRUCTIONS:
# - You have access to a tool named `crop_image(x0, y0, x1, y1, scale)`.
# - If you scan the full form and notice that certain important fields (such as handwritten names, ID numbers, central numbers, or commitment numbers) are unclear, blurry, or located inside small stamps/stickers, do NOT guess.
# - Instead, use the `crop_image` tool by providing fractional coordinates (0.0 to 1.0) and an appropriate scale factor (e.g., 4) to get a zoomed-in, high-resolution view of that specific region.
# - Analyze the cropped image returned by the tool. You may perform multiple crops if necessary across different parts of the form.
# - Once you have gathered and verified all possible data with high confidence, output the final structured JSON object. If a field remains illegible even after cropping, return `null` (or `[]` for lists) and add its key to `fields_requiring_review`.
# """
PROMPT += """
### AGENT LOOP & DYNAMIC CROPPING INSTRUCTIONS
These instructions take precedence over EXTRACTION RULES 1-2 above where they differ.

You have a tool, `crop_image(x0, y0, x1, y1, scale, fields)`, that returns a zoomed-in,
high-resolution view of part of the form. Coordinates are fractions of the page (0.0-1.0);
use scale 3-6. In `fields`, list the JSON keys the crop is meant to read.

**How your answer is judged:** the fewer fields you return as null or [], the better - as long as
every value was actually read from the image. A null means you failed to read a field, not that
you played it safe. But a guessed value is worse than a null: never guess, never fill in missing
digits, and never "fix" a name or number to what seems likely. Every value must be something you
can see in the image.

**Required process:**
1. Scan the full page and find where each field is, or where it is likely to be: the patient
   name and ID area, every "מרכזי" sticker, the "התקבל" stamp, the commitment / דרישה / דגימה
   number, and the sender's logo or letterhead.
2. Crop before you conclude. You may NOT return null or [] for a field unless you have cropped
   the region where it should be (with that key in `fields`) and still could not read it. If you
   don't know where a field is, crop the likely areas (stickers, stamps, handwritten areas) to
   search for it.
3. Crop handwritten values, ID numbers, and numbers on stickers and stamps even if they look
   readable in the full view, and check them digit by digit in the zoomed view.
4. Request several crops in the same turn when you can. If a crop is still unclear, crop again
   tighter or at a higher scale. Do not stop after one look.
5. If you could read a value but are unsure about a character, fill it in and add its key to
   `fields_requiring_review` - do not return null for it. Only a value you cannot read at all,
   after cropping, stays null (or []) and goes into `fields_requiring_review`.
6. When every field is either read or cropped and found missing or illegible, output the final
   JSON object only.
"""

# # REST and not the default gRPC: when the connection fails (e.g. a TLS error) gRPC retries for
# # minutes, so the except below would never get to return the error.
# genai.configure(api_key=os.getenv("GOOGLE_API_KEY") or None, transport="rest")

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
# claude-3-5-sonnet-20241022 was retired on 2025-10-28; the current Sonnet is the default.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"


# def extract_full_image_with_claude(image_bytes: bytes) -> str:
#     """Send the full page image with PROMPT to Claude and return its reply (a JSON string)."""
#     image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
#     response = client.beta.messages.create(
#         # "or" and not a getenv default: an empty CLAUDE_MODEL= line in .env must fall back too.
#         model=os.getenv("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
#         max_tokens=16000,
#         # If a safety classifier declines, the API retries on its recommended fallback model.
#         betas=["server-side-fallback-2026-07-01"],
#         fallbacks="default",
#         messages=[{
#             "role": "user",
#             "content": [
#                 {
#                     "type": "image",
#                     "source": {"type": "base64", "media_type": "image/png", "data": image_data},
#                 },
#                 {"type": "text", "text": PROMPT},
#             ],
#         }],
#     )
#     if response.stop_reason == "refusal":
#         raise RuntimeError("Claude declined to process this page")
#     if response.stop_reason == "max_tokens":
#         raise RuntimeError("Claude's reply was cut off (max_tokens)")
#     return "".join(block.text for block in response.content if block.type == "text")


# Lets Claude zoom into part of the page (e.g. a handwritten field) and read it at higher resolution.
# CLAUDE_TOOLS = [
#     {
#         "name": "crop_image",
#         "description": "Crop a region of the image and upscale it. Coordinates are fractions 0.0-1.0",
#         "input_schema": {
#             "type": "object",
#             "properties": {
#                 "x0": {"type": "number"},
#                 "y0": {"type": "number"},
#                 "x1": {"type": "number"},
#                 "y1": {"type": "number"},
#                 "scale": {"type": "integer"},
#             },
#             "required": ["x0", "y0", "x1", "y1"],
#         },
#     }
# ]

# The JSON keys the model must fill (fields_requiring_review is not one of them).
DATA_FIELDS = [
    "first_name", "last_name", "id_number", "central_numbers",
    "received_date", "sending_entity", "commitment_numbers",
]

# Same tool plus "fields": which keys a crop is for, so the loop can check that every field
# returned empty was cropped first.
CLAUDE_TOOLS = [
    {
        "name": "crop_image",
        "description": (
            "Crop a region of the image and upscale it. Coordinates are fractions 0.0-1.0. "
            "List in `fields` the JSON keys this crop is meant to read."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x0": {"type": "number"},
                "y0": {"type": "number"},
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "scale": {"type": "integer"},
                "fields": {"type": "array", "items": {"type": "string", "enum": DATA_FIELDS}},
            },
            "required": ["x0", "y0", "x1", "y1"],
        },
    }
]

# The API rejects images over 8000 px per side, and bigger images cost more tokens - so the
# scale is lowered when the upscaled crop would be longer than this on its longest side.
MAX_CROP_SIDE = 2000


def crop_image(image_bytes: bytes, x0: float, y0: float, x1: float, y1: float, scale: int = 4) -> bytes:
    """Crop the region given as fractions (0.0-1.0) of the image, upscale it, return PNG bytes.

    The input comes from the model, so it is clamped and ordered rather than trusted.
    """
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode image")
    height, width = image.shape[:2]

    x0, x1 = sorted(min(max(v, 0.0), 1.0) for v in (x0, x1))
    y0, y1 = sorted(min(max(v, 0.0), 1.0) for v in (y0, y1))
    left, right = int(x0 * width), int(x1 * width)
    top, bottom = int(y0 * height), int(y1 * height)
    if right <= left or bottom <= top:
        raise ValueError("crop region is empty")

    crop = image[top:bottom, left:right]
    scale = max(1, int(scale))
    scale = max(1, min(scale, MAX_CROP_SIDE // max(crop.shape[:2])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        raise ValueError("could not encode cropped image")
    return encoded.tobytes()


# MAX_AGENT_TURNS = 8
# More turns now that every empty field must be cropped first, and the loop can send Claude back.
MAX_AGENT_TURNS = 12
# How many times the loop sends Claude back to crop fields it returned empty without cropping.
MAX_CROP_REMINDERS = 2


def _uncropped_empty_fields(result: dict, cropped_fields: set[str]) -> list[str]:
    """Fields returned as null / "" / [] whose region Claude never cropped."""
    return [f for f in DATA_FIELDS if result.get(f) in (None, "", []) and f not in cropped_fields]


def _parse_json_reply(text: str) -> dict:
    """Parse the model's final JSON, tolerating a ```json fence around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


def extract_with_agent_loop(image_bytes: bytes) -> dict:
    """Send the full page to Claude and let it zoom in with crop_image until it can answer.

    Returns the parsed JSON fields. Raises RuntimeError if Claude declines, is cut off, or
    doesn't finish within MAX_AGENT_TURNS.
    """
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
            {"type": "text", "text": PROMPT},
        ],
    }]
    cropped_fields: set[str] = set()
    reminders = 0
    for _ in range(MAX_AGENT_TURNS):
        response = client.messages.create(
            # "or" and not a getenv default: an empty CLAUDE_MODEL= line in .env must fall back too.
            model=os.getenv("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
            # Thinking tokens count toward max_tokens, so 1500 could cut the reply off mid-JSON.
            max_tokens=16000,
            tools=CLAUDE_TOOLS,
            messages=messages,
        )
        # Debug: block types and crop coordinates only - never block text, which holds form data.
        print("DEBUG - Claude stop_reason:", response.stop_reason)
        for block in response.content:
            print("DEBUG - block:", block.type)
            if block.type == "tool_use":
                print("DEBUG -   tool:", block.name, block.input)
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to process this page")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's reply was cut off (max_tokens)")
        # The full content (thinking blocks included) goes back unchanged on the next request.
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use" and b.name == "crop_image"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            # return _parse_json_reply(text)
            result = _parse_json_reply(text)
            missing = _uncropped_empty_fields(result, cropped_fields)
            if not missing or reminders >= MAX_CROP_REMINDERS:
                return result
            # An empty field is only allowed after its region was cropped - send Claude back.
            reminders += 1
            print("DEBUG - empty without crop, asking again:", missing)
            messages.append({"role": "user", "content": (
                "You returned these fields as null or [] without cropping their region first: "
                f"{', '.join(missing)}. Use crop_image on the regions where they should appear "
                "(put the keys in `fields`), read them in the zoomed view, and then output the full "
                "JSON again. Never guess - if a field is still unreadable after cropping, it may "
                "stay null."
            )})
            continue

        # Every tool_use in this turn needs its tool_result, all in one user message.
        tool_results = []
        for block in tool_uses:
            args = block.input
            try:
                cropped = crop_image(
                    image_bytes, args["x0"], args["y0"], args["x1"], args["y1"], args.get("scale", 4)
                )
            except (KeyError, TypeError, ValueError) as exc:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"crop failed: {exc}",
                })
                continue
            # Only a crop that succeeded counts as having looked at these fields.
            cropped_fields.update(f for f in args.get("fields") or [] if f in DATA_FIELDS)
            cropped_base64 = base64.standard_b64encode(cropped).decode("utf-8")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                # crop_image returns PNG, so the media type must say PNG.
                "content": [{
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": cropped_base64},
                }],
            })
        messages.append({"role": "user", "content": tool_results})

    logger.error("agent loop did not finish within %d turns", MAX_AGENT_TURNS)
    raise RuntimeError(f"Claude did not finish within {MAX_AGENT_TURNS} turns")


# def classify_entity(model, image) -> str | None:
#     """Asks the model to identify the issuing entity in one pass: visual logo/letterhead first,
#     falling back to the entity's name elsewhere in the page's text (see
#     ENTITY_CLASSIFICATION_PROMPT). Returns the entity name if it's an exact match for one of our
#     closed-list keys, otherwise None (covers both an explicit NONE reply and any unexpected text)."""
#     reply = model.generate_content([ENTITY_CLASSIFICATION_PROMPT, image]).text.strip()
#     return reply if reply in ENTITY_TEMPLATES else None


# def extract_data_from_image(image_path: str) -> str:
#     try:
#         # "or" and not a getenv default: an empty MODEL_NAME= line in .env must fall back too.
#         model = genai.GenerativeModel(os.getenv("MODEL_NAME") or DEFAULT_MODEL)
#         with Image.open(image_path) as image:
#             entity = classify_entity(model, image)

#             prompt = PROMPT
#             if entity:
#                 prompt += (
#                     "\n\n### AUTOMATIC HINT (not verified, may be wrong):\n"
#                     f"Automatic classification suggests the sending entity may be: {entity}.\n"
#                     "Use this only as a starting point. Confirm sending_entity yourself from what "
#                     "is actually printed on the page, and ignore this hint if it conflicts with "
#                     "what you see.\n"
#                 )
#                 hints = ENTITY_TEMPLATES.get(entity, {}).get("hints", "")
#                 if hints:
#                     prompt += (
#                         f"Known layout notes for {entity} (also just a hint, verify against the "
#                         f"image):\n{hints}\n"
#                     )

#             response = model.generate_content([prompt, image])
#         return response.text
#     except Exception as exc:
#         logger.error("image extraction failed: %s", type(exc).__name__)
#         return f"{ERROR_PREFIX}{exc}"


# FIELD_PROMPT = (
#     "This is a cropped image of the field '{field_name}' from a Hebrew form. It may be handwritten, "
#     "printed, or empty. Read the text. If it is empty or illegible, return 'null'. Return ONLY the "
#     "extracted value without any markdown, explanations, or extra text."
# )


# def extract_fields_with_gemini(cropped_images: dict[str, bytes]) -> dict:
#     """Read each cropped field image with Gemini. Returns {field_name: text, or None if empty}.

#     API errors are raised, not swallowed: a failed call must not look like an empty field.
#     """
#     model = genai.GenerativeModel(os.getenv("MODEL_NAME") or DEFAULT_MODEL)
#     results: dict[str, str | None] = {}
#     for field_name, image_bytes in cropped_images.items():
#         with Image.open(io.BytesIO(image_bytes)) as image:
#             response = model.generate_content([FIELD_PROMPT.format(field_name=field_name), image])
#         text = response.text.strip().strip("'\"`").strip()
#         # A missing field stays empty - never a guessed value or the literal word "null".
#         results[field_name] = None if not text or text.lower() == "null" else text
#     return results
