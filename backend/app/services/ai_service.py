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

# .env lives in the project root (CLAUDE.md section 8) - the same file processor.py loads.
ROOT = Path(__file__).resolve().parents[3]
# The API is configured at import time, before main.py's load_dotenv() call can run.
load_dotenv(ROOT / ".env")

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


def get_entity_hints(entity_name: str) -> str:
    """Looks up known form-layout hints for one sending entity (hospital or HMO).

    Exposed to Claude as a tool (see ENTITY_HINTS_TOOL): once it has classified the sending
    entity it asks for that entity's recorded notes - where the commitment number is labeled, or
    a field this entity's forms never have - before extracting the remaining fields.

    Returns the recorded layout hints for that entity, or an empty string if none are recorded
    or the name isn't recognized.
    """
    return ENTITY_TEMPLATES.get(entity_name, {}).get("hints", "")


# Each entity's own name plus its documented aliases, normalized, mapped back to the canonical
# name used in the closed list.
_ALIAS_TO_ENTITY = {
    alias.casefold(): name
    for name, template in ENTITY_TEMPLATES.items()
    for alias in [name, *template.get("aliases", [])]
}
_ENTITY_ALIASES = {
    name: [alias.casefold() for alias in [name, *template.get("aliases", [])]]
    for name, template in ENTITY_TEMPLATES.items()
}


def _flag(data: dict, field: str) -> None:
    """Adds a field to fields_requiring_review, repairing the key if the model dropped it."""
    flags = data.get("fields_requiring_review")
    if not isinstance(flags, list):
        flags = []
        data["fields_requiring_review"] = flags
    if field not in flags:
        flags.append(field)


def _evidence_names(entity: str, evidence) -> bool:
    """True if the wording the model copied off the page actually names this entity."""
    if not isinstance(evidence, str):
        return False
    quoted = evidence.casefold()
    return any(alias in quoted for alias in _ENTITY_ALIASES[entity])


def _enforce_closed_list(data: dict) -> bool:
    """Enforces the closed list of sending entities (data model field 6) in code, in place.

    The prompt forbids a value outside the list, but faced with a sender that simply isn't on it
    (a foreign payer, a government authority, a private lab) the model still tends to fill in the
    closest-looking HMO rather than leave the field empty - and a plausible wrong name is worse
    than an empty one, since nothing marks it as invented. A known alias is mapped to its
    canonical name; a value that is off the list, or that the model could not point to anywhere
    on the page, is cleared and flagged for review.

    Returns True if sending_entity was cleared here.
    """
    cleared = False
    entity = data.get("sending_entity")
    if isinstance(entity, str) and entity.strip():
        canonical = _ALIAS_TO_ENTITY.get(entity.strip().casefold())
        # A name on the list is not enough: the model also hallucinates listed names (a screenshot
        # with no letterhead at all came back as a confident HMO name). The quote it had to copy
        # from the page must actually contain the entity's name, or the classification had nothing
        # behind it.
        if canonical is not None and not _evidence_names(canonical, data.get("sending_entity_evidence")):
            canonical = None
        if canonical != entity:
            data["sending_entity"] = canonical
            if canonical is None:
                cleared = True
                _flag(data, "sending_entity")
                logger.info("sending_entity cleared: not on the closed list, or not shown on the page")

    # sending_entity_evidence only exists to ground the classification above - it is not one of
    # the extracted fields, so it has no business in the reviewer's list of fields to check.
    flags = data.get("fields_requiring_review")
    if isinstance(flags, list) and "sending_entity_evidence" in flags:
        data["fields_requiring_review"] = [f for f in flags if f != "sending_entity_evidence"]
    return cleared

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
2. CONFIDENCE: score every field from 0 to 100 in the "confidence" object, under the same JSON
   key - how sure you are that the value you wrote is exactly what is on the page.
   - 50 or more: WRITE THE VALUE, even though you are not fully sure. A reading a human can check
     is worth more than an empty field, so do not drop a value just because it is imperfect.
   - Below 50: you did not really read it - leave the field null (or []). A field that is simply
     not on the page scores 0.
   - Every field you did not score 100 must also be listed in `fields_requiring_review`: it is
     shown to the reviewer in red, with its score, for checking against the image. 100 means you
     are certain, character for character.
3. MULTIPLE VALUES: Fields defined as arrays may contain multiple numbers. Extract and save all of them.

### STEP 1 - CLASSIFY THE SENDING ENTITY FIRST:
Before extracting anything else, identify which institution (hospital or health fund / קופת
חולים) ISSUED / SENT this form:
1. First, look for a visual logo, letterhead graphic, or stamp at the top of the page. If you can
   confidently recognize the issuing institution from it, use that.
2. Otherwise, look for the institution's name written elsewhere on the page as part of the
   letterhead, header, stamps, or codes identifying the sender.
2a. GROUNDING - the classification must rest on the institution's name actually appearing on the
   page (printed text, stamp, or the wording inside a logo). Copy that wording verbatim into
   "sending_entity_evidence". Many of these pages are plain internal-system screenshots with no
   letterhead at all: the type of form, its layout, or the fact that most forms come from some
   HMO are NOT evidence. If you cannot quote wording that names the institution, you have not
   identified it - leave sending_entity null and flag it.
3. IMPORTANT: you are identifying the issuing/sending institution, not the patient's own health
   fund membership. A field such as "קופת חולים" listed under the PATIENT's personal details
   (alongside the patient's name, ID number, etc.) describes which HMO the patient belongs to as
   a person - it is NOT the sender of the form and must be ignored for this purpose.
4. The issuer is sometimes a body that is not an Israeli HMO or hospital at all - a government
   office, an authority or foreign payer (e.g. "הרשות הפלסטינאית"), an insurance company, a
   private clinic or an external laboratory. Such a body is NOT on the closed list, and there is
   no listed value that stands for it: leave sending_entity null and add it to
   `fields_requiring_review`. Never substitute a listed HMO because it is the most common one,
   because its name partly resembles what is written, or because a value "has to" be filled in.
   For this one field an empty, flagged value is a correct answer and costs you nothing - this
   overrides the scoring note in the cropping instructions below.
5. If you still cannot confidently determine the issuing institution, leave sending_entity null
   and add it to `fields_requiring_review` - do not call the hints tool in that case.

Once you have a confident match, call the `get_entity_hints` tool with that exact entity name to
retrieve any known layout notes for it, and use what it returns (if anything) while extracting
the remaining fields below. Call it at most once.

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
- "sending_entity" (String): גורם שולח / קופת חולים / בית חולים, as classified in STEP 1 above.
  Must be copied exactly as one of the values in this closed list - never guess or invent a value
  outside it: <<ENTITY_LIST>>. If the form clearly names an entity that is not in this list, leave
  this field null and add "sending_entity" to `fields_requiring_review` instead of forcing the
  closest match. An empty flagged field is the CORRECT answer here - a wrong listed name is worse
  than no name, because a reviewer has no way to tell it was invented.
- "sending_entity_evidence" (String): the exact wording on the page that names the sending
  entity, copied character for character as it appears (e.g. a letterhead line, a stamp, or the
  text inside a logo). Never paraphrase it, never describe the form instead, and never write a
  name here that is not printed on the page. null if sending_entity is null.
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
  "sending_entity_evidence": null,
  "commitment_numbers": [],
  "confidence": {
    "first_name": 0,
    "last_name": 0,
    "id_number": 0,
    "central_numbers": 0,
    "received_date": 0,
    "sending_entity": 0,
    "commitment_numbers": 0
  },
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
4. Request several crops in the same turn when you can, each one aimed at a different spot on
   the page. If a crop is still unclear, crop again tighter or at a higher scale. Do not stop
   after one look.
4a. One crop reads one spot. A crop must be tight around the value you are reading - a sticker,
   a stamp, a name line - and must stay under 6% of the page area. A wide band of the page is a
   survey: use it to FIND where a field is, but it does not count as having read anything, and
   you will be sent back to crop that spot on its own. Do not list several fields in `fields`
   unless they are printed together in that same small area.
5. If you could read a value but are unsure about a character, fill it in, score it honestly in
   "confidence" (anything from 50 up is kept, not dropped) and add its key to
   `fields_requiring_review` - do not return null for it. Only a value you cannot read at all,
   after cropping, stays null (or []) with a score below 50, and goes into
   `fields_requiring_review`.
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

# The hints lookup as a tool, so the model fetches the layout notes for the entity it actually
# classified (STEP 1 of the prompt) instead of us pasting all of them into every prompt.
ENTITY_HINTS_TOOL = {
    "name": "get_entity_hints",
    "description": (
        "Return known form-layout notes for one sending entity (hospital or HMO). Call this once, "
        "right after identifying sending_entity, passing that same value - before extracting the "
        "remaining fields. Returns a note that this entity has none on record if nothing is."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_name": {
                "type": "string",
                "description": "The sending entity, copied exactly from the closed list.",
                "enum": list(ENTITY_TEMPLATES.keys()),
            },
        },
        "required": ["entity_name"],
    },
}

# The JSON keys the model must fill (fields_requiring_review and sending_entity_evidence are not
# among them - the evidence only grounds sending_entity, see _enforce_closed_list).
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
    },
    ENTITY_HINTS_TOOL,
]

# The API rejects images over 8000 px per side, and bigger images cost more tokens - so the
# scale is lowered when the upscaled crop would be longer than this on its longest side.
MAX_CROP_SIDE = 2000

# Largest share of the page a crop may cover and still count as having READ its fields (see the
# agent loop). A sticker, a stamp or a name line is a few percent of a page; a crop much larger
# than that is a survey of a whole band, and a value missed inside it was never really looked at.
MAX_READ_CROP_AREA = 0.06


def _crop_area(args: dict) -> float:
    """The fraction of the page a crop covers, clamped the same way crop_image clamps it."""
    try:
        x0, x1 = sorted(min(max(float(args[k]), 0.0), 1.0) for k in ("x0", "x1"))
        y0, y1 = sorted(min(max(float(args[k]), 0.0), 1.0) for k in ("y0", "y1"))
    except (KeyError, TypeError, ValueError):
        return 1.0
    return (x1 - x0) * (y1 - y0)


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
# Three, not two: a wide survey crop no longer counts as a read, so a legitimate run may need a
# locate pass and a zoom pass before a field is genuinely settled.
MAX_CROP_REMINDERS = 3


def _conf_threshold(name: str, default: int) -> int:
    """Reads a confidence threshold from .env, falling back on a missing, empty or invalid value."""
    try:
        return max(0, min(100, int(os.getenv(name) or default)))
    except ValueError:
        logger.error("%s is not a number - using %d", name, default)
        return default


# Confidence bands (CLAUDE.md section 5), tunable in .env. A value scored below CONF_EMPTY_BELOW
# is not kept at all, and the table shows that row in red; from there up to and including
# CONF_OK_ABOVE it IS written, but flagged, and the table shows it in orange with its score;
# only a score above CONF_OK_ABOVE passes unflagged.
# The default 99 means "only a certain reading passes clean"; set CONF_OK_ABOVE=90 in .env for the
# threshold the specification suggests.
CONF_EMPTY_BELOW = _conf_threshold("CONF_EMPTY_BELOW", 50)
CONF_OK_ABOVE = _conf_threshold("CONF_OK_ABOVE", 99)


def _score(value) -> int | None:
    """The model's score for one field as an int 0-100, or None if it didn't give a usable one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(value)))


def _apply_confidence(data: dict) -> None:
    """Applies the confidence bands to the model's own scores, in place.

    A field scored 50 or more stays filled - that is the point of asking for a score rather than
    a yes/no: a partly-sure reading reaches the reviewer in red instead of being thrown away.
    Below CONF_EMPTY_BELOW the value is dropped (it was not really read), and anything that is not
    a certain reading is flagged either way.
    """
    scores = data.get("confidence")
    if not isinstance(scores, dict):
        # No scores at all: nothing is confirmed, so every filled field goes to the reviewer.
        logger.info("model returned no confidence scores - flagging every filled field")
        scores = {}

    applied: dict[str, int] = {}
    for field in DATA_FIELDS:
        empty = data.get(field) in (None, "", [])
        score = _score(scores.get(field))
        if score is None:
            if not empty:
                _flag(data, field)
            continue
        if score < CONF_EMPTY_BELOW and not empty:
            data[field] = [] if isinstance(data[field], list) else None
            empty = True
            logger.info("field dropped: confidence %d is below %d", score, CONF_EMPTY_BELOW)
        # Nothing was kept, so there is no reading to be confident about.
        if empty:
            score = 0
        if score <= CONF_OK_ABOVE:
            _flag(data, field)
        applied[field] = score
    data["confidence"] = applied
    # The table colours each row by its score, so it needs the thresholds that produced it -
    # they are tunable in .env and must not be duplicated as constants in the frontend.
    data["thresholds"] = {"empty_below": CONF_EMPTY_BELOW, "ok_above": CONF_OK_ABOVE}


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

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            # return _parse_json_reply(text)
            result = _parse_json_reply(text)
            # The closed list is enforced here in code, not left to the model.
            if _enforce_closed_list(result):
                # We emptied the field on purpose - don't then send Claude back to crop for it.
                cropped_fields.add("sending_entity")
            # A field read with 50+ confidence is kept and flagged, not dropped (see the bands).
            _apply_confidence(result)
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
            if block.name == ENTITY_HINTS_TOOL["name"]:
                hints = get_entity_hints((block.input or {}).get("entity_name", ""))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    # An empty string is not valid tool_result content, and silence would read as
                    # a failed lookup - say plainly that there is nothing on record.
                    "content": hints or "No layout notes are on record for this entity.",
                })
                continue
            if block.name != "crop_image":
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"unknown tool: {block.name}",
                })
                continue
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
            cropped_base64 = base64.standard_b64encode(cropped).decode("utf-8")
            # crop_image returns PNG, so the media type must say PNG.
            content = [{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": cropped_base64},
            }]
            # Only a crop that succeeded AND is tight enough to read from counts as having looked
            # at these fields. A wide crop is a survey: it is still returned (finding where a
            # field sits is a legitimate use), but it leaves the field "not looked at", so the
            # reminder below sends Claude back to zoom in on it properly.
            claimed = [f for f in args.get("fields") or [] if f in DATA_FIELDS]
            if _crop_area(args) <= MAX_READ_CROP_AREA:
                cropped_fields.update(claimed)
            elif claimed:
                content.append({"type": "text", "text": (
                    f"This crop covers {_crop_area(args):.0%} of the page - too wide to read a "
                    f"value from, so it does not count as having read {', '.join(claimed)}. "
                    "Use it to locate the field, then crop that spot on its own, tightly."
                )})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
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
