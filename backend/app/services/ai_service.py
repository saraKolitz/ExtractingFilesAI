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
import os
from anthropic import Anthropic

from .barcode import apply_barcode_check, decode_barcodes
from .orientation import detect_rotation, rotate_image, tesseract_rotation, unrotate_box
from .validation import validate_extraction

# .env lives in the project root (CLAUDE.md section 8) - the same file processor.py loads.
ROOT = Path(__file__).resolve().parents[3]
# The API is configured at import time, before main.py's load_dotenv() call can run.
load_dotenv(ROOT / ".env")

# Logs must never contain personal data (file names, content) - only error types.
logger = logging.getLogger("uvicorn.error")


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
# only a score above CONF_OK_ABOVE passes unflagged. So with the defaults: 0-14 dropped, 15-70 kept
# and flagged, 71-100 kept clean.
# Policy: a reading the model is under 15% sure of is not worth showing - the cell stays empty
# (NULL on screen) rather than offering the reviewer a near-random value. Anything from 15% up is
# written and flagged for checking. Change CONF_EMPTY_BELOW in .env to move that bar.
# Defined before the prompts because the prompts tell the model these same numbers.
DEFAULT_CONF_EMPTY_BELOW = 15
DEFAULT_CONF_OK_ABOVE = 70
CONF_EMPTY_BELOW = _conf_threshold("CONF_EMPTY_BELOW", DEFAULT_CONF_EMPTY_BELOW)
CONF_OK_ABOVE = _conf_threshold("CONF_OK_ABOVE", DEFAULT_CONF_OK_ABOVE)


def _with_thresholds(prompt: str) -> str:
    """Fills the confidence thresholds into a prompt, so it never states different numbers than
    the code applies (<<EMPTY_BELOW>> is CONF_EMPTY_BELOW, <<OK_ABOVE>> is CONF_OK_ABOVE)."""
    return prompt.replace("<<EMPTY_BELOW>>", str(CONF_EMPTY_BELOW)).replace("<<OK_ABOVE>>", str(CONF_OK_ABOVE))


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

    Used by build_extraction_prompt: once classify_entity has settled the sender, that entity's
    recorded notes - where the commitment number is labeled, or a field its forms never have -
    are added to the extraction prompt.

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

# Classification is its own call, made before the extraction (see classify_entity): try the visual
# logo/letterhead first (survives noisy OCR and handwriting), and only fall back to the entity's
# name elsewhere in the page's text if no logo can be matched confidently. The reply carries the
# wording it was based on, which _enforce_closed_list checks against the entity's known names.
ENTITY_CLASSIFICATION_PROMPT = """Look at this image of a Hebrew medical form and identify which
institution (hospital or health fund / קופת חולים) ISSUED / SENT it. This is the only thing you are
asked to do - do not read or extract anything else from the page.

Closed list of possible values, copied exactly as written: <<ENTITY_LIST>>

Follow these steps, in order:
1. Look for a visual logo, letterhead graphic, or stamp at the top of the page. If you can
   confidently recognize the issuing institution from it, use that.
2. If there is no visible logo, or it does not identify the institution, look for the
   institution's name written anywhere in the text on the page (letterhead text, header, stamps,
   codes identifying the sender) and identify it that way.
3. GROUNDING - the answer must rest on the institution's name actually appearing on the page
   (printed text, stamp, or the wording inside a logo). Copy that wording verbatim into "evidence".
   Many of these pages are plain internal-system screenshots with no letterhead at all: the type
   of form, its layout, or the fact that most forms come from some HMO are NOT evidence. If you
   cannot quote wording that names the institution, you have not identified it.
4. You are identifying the issuing institution, not the patient's own health fund membership. A
   field such as "קופת חולים" listed under the PATIENT's personal details (alongside the patient's
   name, ID number, etc.) describes which HMO the patient belongs to as a person - it is NOT the
   sender of the form and must be ignored.
5. The issuer is sometimes a body that is not on the list at all - a government office, an
   authority or foreign payer (e.g. "הרשות הפלסטינאית"), an insurance company, a private clinic or
   an external laboratory. Never substitute a listed HMO because it is the most common one,
   because its name partly resembles what is written, or because a value "has to" be filled in.
   If no wording on the page names an institution on the list, the answer is null: a wrong listed
   name is worse than none, because nothing marks it as invented.
6. If you CAN quote wording on the page that names an institution on the list, answer with it even
   when you are not sure - score it honestly, however low. The quote is what keeps the answer
   honest, so an unsure answer with a quote is better than null.

Respond with ONLY a JSON object - no Markdown, no explanation, nothing else:
{"entity": <exactly one value copied from the list, or null>, "evidence": <the wording quoted from the page, or null>, "confidence": <0-100>}
"confidence" is how sure you are that the entity is right. Use null for both "entity" and
"evidence" only when you found no wording naming an institution on the list; then score 0. A score
below <<EMPTY_BELOW>> makes the code discard the entity.
"""
ENTITY_CLASSIFICATION_PROMPT = _with_thresholds(ENTITY_CLASSIFICATION_PROMPT).replace(
    "<<ENTITY_LIST>>", ", ".join(ENTITY_TEMPLATES.keys())
)

# The extraction prompt is built per page from three parts (see build_extraction_prompt): this
# head (rules and fields, the same for every form), a section that depends on who sent the form
# (_entity_section), and this tail (output format and zoom instructions, also the same for every
# form). The sender itself is not extracted here - classify_entity settles it first.
_PROMPT_HEAD = """You are an expert AI data extraction system specialized in scanning Hebrew administrative forms.
Your task is to extract specific data fields from the provided image and return ONLY a valid, structured JSON object. Do not include any Markdown formatting, explanations, or conversational text outside the JSON.

### EXTRACTION RULES:
1. STRICT ACCURACY: Do NOT invent information - never write a value that is not visible on the page. If a field is not present, or nothing of it can be made out, return `null` for strings or an empty array `[]` for lists. A value you can make out only partly or doubtfully is NOT "cannot be read": write your best reading of what is visible and score it low (rule 2).
2. CONFIDENCE: score every field from 0 to 100 in the "confidence" object, under the same JSON
   key - how sure you are that the value you wrote is exactly what is on the page.
   - FOUND the field: ALWAYS WRITE THE VALUE - your best reading of what is on the page - however
     unsure you are, even if you would put the score at only a few percent. A reading a human can
     check is worth far more than an empty field, so never leave a field empty because you are
     not sure of it. Scores of <<EMPTY_BELOW>> and up are all kept.
   - Did NOT find the field (it is not on the page, or you looked where it should be and there is
     nothing there): leave it null (or []) and score it 0. A score below <<EMPTY_BELOW>> makes the
     code throw the value away, so never score 0 for something you wrote.
   - Every field you score <<OK_ABOVE>> or below must also be listed in `fields_requiring_review`:
     it is shown to the reviewer, with its score, for checking against the image. Only a score
     above <<OK_ABOVE>> passes without a check, so score honestly: 100 means you are certain,
     character for character.
3. MULTIPLE VALUES: Fields defined as arrays may contain multiple numbers. Extract and save all of them.

### FIELDS TO EXTRACT:
- "first_name" (String): The private name of the patient.
- "last_name" (String): The family name of the patient.
- "id_number" (String): The ID number (תעודת זהות).
- "central_numbers" (Array of Strings): מספר מרכזי. STRICT LABEL MATCH: copy a number into this
  array ONLY if it is explicitly tied to the label "מרכזי" (central) - whether printed on a sticker
  or handwritten next to/under that label. A number on a sticker, or anywhere else on the page,
  that is NOT labeled "מרכזי" must NOT be copied here, no matter how similar it looks to a central
  number. FORMAT IS ONLY A HINT: a central number usually sits right next to its label and is
  usually 6 digits (for example 93xxxx or 94xxxx), but the LABEL is what makes a number a central
  number, never its digits - a 6-digit number in that range with no "מרכזי" label is not one, and
  a labeled number in another format still is. LOOK FOR THE LABEL CAREFULLY: the word "מרכזי" is
  small and is very often printed vertically, sideways or rotated (90 degrees in either direction,
  or tilted) beside the barcode, so in the full view it is easy to mistake for a stray mark or
  to miss. Check EVERY barcode and every 6-digit number, usually 93xxxx or 94xxxx, for it: crop
  each one with a margin on all four sides and read the crop turned to every orientation. Whenever
  "מרכזי" appears near a barcode or such a number, in any orientation, the number belongs in this
  array - extract it. This is about reading a label that is there, not about accepting a number
  that has none. NOT ALWAYS A BARCODE: it is usually a sticker with a barcode, but on handwritten or
  screen-capture style pages it can be a handwritten number with no barcode at all - do not skip it
  just because there is no barcode. FULL PAGE SCAN: do not stop after finding the first one - scan
  the entire page for every instance labeled "מרכזי", and return ALL of them in this array (there
  is often more than one for the same sample).
- "received_date" (String): תאריך קבלת הדגימה. Look for a stamp saying "התקבל" or a handwritten date, often located under the central number.
- "commitment_numbers" (Array of Strings): מספר התחייבות. There may be multiple. The commitment
  number is not always labeled "התחייבות" - depending on the sending institution it may instead be
  labeled "דרישה" (request) or "דגימה" (sample number); on some forms the sample number is
  explicitly stated to double as the commitment number. Treat these labels as equally valid sources
  for this field, not just for one specific entity.
  DEFAULT RULE (it applies unless the notes for this sender, if any appear below, say otherwise):
  the commitment number is the form's own primary identifying number - the unique document number
  or the barcode sticker put on it by the issuing facility. It is NEVER a "מרכזי" number, the
  patient's ID number, an internal case / visit / admission number (such as "מקרה"), a phone
  number, or a date. If you cannot tell for sure which number is the commitment number, write the
  likeliest candidate and give it a low score in "confidence" - but never a number those
  exclusions rule out. Leave the field null / [] (score 0) only if no number on the page could be
  the commitment number.
"""
_PROMPT_HEAD = _with_thresholds(_PROMPT_HEAD)

_PROMPT_TAIL = """
### JSON OUTPUT FORMAT:
{
  "first_name": null,
  "last_name": null,
  "id_number": null,
  "central_numbers": [],
  "received_date": null,
  "commitment_numbers": [],
  "confidence": {
    "first_name": 0,
    "last_name": 0,
    "id_number": 0,
    "central_numbers": 0,
    "received_date": 0,
    "commitment_numbers": 0
  },
  "fields_requiring_review": []
}

### AGENT LOOP & DYNAMIC CROPPING INSTRUCTIONS
These instructions take precedence over EXTRACTION RULES 1-2 above where they differ.

You have a tool, `crop_image(x0, y0, x1, y1, scale, fields)`, that returns a zoomed-in,
high-resolution view of part of the form. Coordinates are fractions of the page (0.0-1.0);
use scale 3-6. In `fields`, list the JSON keys the crop is meant to read.

**How your answer is judged:** the fewer fields you return as null or [], the better - as long as
every value was actually read from the image. A null means you did not find the field, not that
you played it safe: when you found it, write it, however unsure you are, and let the score say so.
But an invented value is worse than a null: never write what you cannot see, never fill in missing
digits, and never "fix" a name or number to what seems likely. Every value must be something you
can see in the image - an unsure reading of what is there is fine, a completion of what is not is
not.

**Required process:**
1. Scan the full page and find where each field is, or where it is likely to be: the patient
   name and ID area, every "מרכזי" sticker, the "התקבל" stamp, and the commitment / דרישה / דגימה
   number.
2. Crop before you conclude. You may NOT return null or [] for a field unless you have cropped
   the region where it should be (with that key in `fields`) and still found nothing there. If you
   don't know where a field is, crop the likely areas (stickers, stamps, handwritten areas) to
   search for it.
3. Crop handwritten values, ID numbers, and numbers on stickers and stamps even if they look
   readable in the full view, and check them digit by digit in the zoomed view. For every barcode
   and every 6-digit sticker number, leave a margin around it in the crop and look at all four
   sides for a "מרכזי" label - it is often small and rotated, and the number is a central number
   only if you find it.
4. Request several crops in the same turn when you can, each one aimed at a different spot on
   the page. If a crop is still unclear, crop again tighter or at a higher scale. Do not stop
   after one look.
4a. One crop reads one spot. A crop must be tight around the value you are reading - a sticker,
   a stamp, a name line - and must stay under 6% of the page area. A wide band of the page is a
   survey: use it to FIND where a field is, but it does not count as having read anything, and
   you will be sent back to crop that spot on its own. Do not list several fields in `fields`
   unless they are printed together in that same small area.
5. If you found a value but are unsure of it - a character, a whole number, even whether it is the
   right field - fill it in, score it honestly in "confidence" (anything from <<EMPTY_BELOW>> up
   is kept, not dropped) and add its key to `fields_requiring_review` - do not return null for
   it. Only a field you did not find at all, after cropping the places it should be, stays null
   (or []) with a score of 0, and goes into `fields_requiring_review`.
6. When every field is either written or cropped and found missing, output the final JSON object
   only.
"""
_PROMPT_TAIL = _with_thresholds(_PROMPT_TAIL)


def _entity_section(entity: str | None) -> str:
    """The part of the extraction prompt that depends on who sent the form.

    The sender was already settled by classify_entity, so the model is told not to extract it. A
    sender with recorded layout notes (entity_templates.json, "hints") gets them here; a sender
    without notes, or one that could not be identified, gets only the general instructions.
    """
    if entity is None:
        return (
            "\n### SENDING ENTITY:\n"
            "The institution that issued this form could not be identified, so no sender-specific "
            "notes apply: use only the general instructions in this prompt. Do not return "
            "sending_entity - it is handled separately.\n"
        )
    section = (
        "\n### SENDING ENTITY (already identified - do not return it):\n"
        f"A separate step identified the issuer of this form as: {entity}. Do not return "
        "sending_entity and do not try to re-identify the sender.\n"
    )
    hints = get_entity_hints(entity).strip()
    if hints:
        section += (
            f"\n### LAYOUT NOTES FOR FORMS FROM {entity}:\n{hints}\n"
            "These notes say where to look and how this sender labels things; they are not values. "
            "For the fields they cover they take precedence over the default rules above. Verify "
            "everything against the image, and if the form in front of you differs from the notes, "
            "trust the image.\n"
        )
    return section


def build_extraction_prompt(entity: str | None) -> str:
    """The extraction prompt for one form: the general prompt plus its sender's layout notes.

    entity is the canonical name from classify_entity, or None when the sender was not
    identified. Either way the general instructions are all there; the notes are only added.
    """
    return _PROMPT_HEAD + _entity_section(entity) + _PROMPT_TAIL


# The general prompt: what a form gets when its sender is unknown or has no notes recorded.
PROMPT = build_extraction_prompt(None)

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

# The JSON keys of the extracted result (fields_requiring_review and sending_entity_evidence are
# not among them - the evidence only grounds sending_entity, see _enforce_closed_list).
DATA_FIELDS = [
    "first_name", "last_name", "id_number", "central_numbers",
    "received_date", "sending_entity", "commitment_numbers",
]
# The ones the extraction call reads off the page. sending_entity is not among them: it is
# settled before that call by classify_entity, so there is nothing to crop for it.
CROP_FIELDS = [f for f in DATA_FIELDS if f != "sending_entity"]

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
                "fields": {"type": "array", "items": {"type": "string", "enum": CROP_FIELDS}},
            },
            "required": ["x0", "y0", "x1", "y1"],
        },
    },
]

# The API rejects images over 8000 px per side, and bigger images cost more tokens - so the
# scale is lowered when the upscaled crop would be longer than this on its longest side.
MAX_CROP_SIDE = 2000

# Largest share of the page a crop may cover and still count as having READ its fields (see the
# agent loop). A sticker, a stamp or a name line is a few percent of a page; a crop much larger
# than that is a survey of a whole band, and a value missed inside it was never really looked at.
MAX_READ_CROP_AREA = 0.06


def _crop_box(args: dict) -> list[float] | None:
    """A crop's [x0, y0, x1, y1] as page fractions, clamped and ordered like crop_image does."""
    try:
        x0, x1 = sorted(min(max(float(args[k]), 0.0), 1.0) for k in ("x0", "x1"))
        y0, y1 = sorted(min(max(float(args[k]), 0.0), 1.0) for k in ("y0", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _crop_area(args: dict) -> float:
    """The fraction of the page a crop covers, clamped the same way crop_image clamps it."""
    box = _crop_box(args)
    return 1.0 if box is None else (box[2] - box[0]) * (box[3] - box[1])


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


def _score(value) -> int | None:
    """The model's score for one field as an int 0-100, or None if it didn't give a usable one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(value)))


def _apply_confidence(data: dict) -> None:
    """Applies the confidence bands to the model's own scores, in place.

    A field scored CONF_EMPTY_BELOW or more stays filled - that is the point of asking for a score
    rather than a yes/no: a partly-sure reading reaches the reviewer instead of being thrown away.
    Below CONF_EMPTY_BELOW the value is dropped (with the default 15 that is a score of 0-14,
    which includes the 0 the model gives a field it did not find). Up to and including CONF_OK_ABOVE a field is
    flagged for review; only a score above it passes clean.

    fields_requiring_review is rebuilt here from the scores, not taken from the model: the model
    lists what it doubts, and a field it doubted but scored above CONF_OK_ABOVE would otherwise
    stay flagged and the threshold would not mean what it says. The validation rules run after
    this and add their own flags.
    """
    scores = data.get("confidence")
    if not isinstance(scores, dict):
        # No scores at all: nothing is confirmed, so every filled field goes to the reviewer.
        logger.info("model returned no confidence scores - flagging every filled field")
        scores = {}
    data["fields_requiring_review"] = []

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


def _model_name() -> str:
    # "or" and not a getenv default: an empty CLAUDE_MODEL= line in .env must fall back too.
    return os.getenv("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL


def _resolve_classification(reply) -> tuple[str | None, str | None, int | None]:
    """Turns the model's classification reply into (entity, evidence, confidence).

    The same closed-list rules as before apply, now to the classification alone: the name must be
    on the list and the wording the model quoted from the page must contain it
    (_enforce_closed_list), and a reading scored below CONF_EMPTY_BELOW counts as not identified.
    An entity that does not pass comes back as (None, None, None), and the form is then extracted
    with the general prompt. The confidence is None when the model gave no usable score.
    """
    if not isinstance(reply, dict):
        return None, None, None
    candidate = {
        "sending_entity": reply.get("entity"),
        "sending_entity_evidence": reply.get("evidence"),
    }
    _enforce_closed_list(candidate)
    entity = candidate["sending_entity"]
    if not isinstance(entity, str) or not entity:
        return None, None, None
    score = _score(reply.get("confidence"))
    if score is not None and score < CONF_EMPTY_BELOW:
        logger.info("sending_entity dropped: confidence %d is below %d", score, CONF_EMPTY_BELOW)
        return None, None, None
    return entity, candidate["sending_entity_evidence"], score


def classify_entity(image_bytes: bytes) -> tuple[str | None, str | None, int | None]:
    """Asks Claude which institution issued the form, before anything else is extracted.

    Returns (entity, evidence, confidence) as _resolve_classification does. The result decides
    which prompt the extraction gets (see build_extraction_prompt). A reply that is not JSON is
    treated as "not identified", so the form still goes through with the general prompt; an API
    failure or a refusal is raised, like in the extraction itself.
    """
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = client.messages.create(
        model=_model_name(),
        # Thinking tokens count toward max_tokens; the JSON itself is only a few dozen.
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
                {"type": "text", "text": ENTITY_CLASSIFICATION_PROMPT},
            ],
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this page")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's reply was cut off (max_tokens)")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        reply = _parse_json_reply(text)
    except ValueError:
        logger.error("entity classification reply was not JSON - using the general prompt")
        return None, None, None
    return _resolve_classification(reply)


def _set_sending_entity(result: dict, entity: str | None, evidence: str | None, score: int | None) -> None:
    """Writes the classification into the extraction result, in place.

    The extraction prompt does not ask for the sender, so this is the only place it comes from.
    The score goes into "confidence" so the bands in _apply_confidence treat it like any field:
    no sender is scored 0 and flagged, and a missing score leaves the sender flagged too.
    """
    result["sending_entity"] = entity
    result["sending_entity_evidence"] = evidence
    scores = result.get("confidence")
    if not isinstance(scores, dict):
        scores = result["confidence"] = {}
    if entity is None:
        scores["sending_entity"] = 0
    elif score is not None:
        scores["sending_entity"] = score
    else:
        scores.pop("sending_entity", None)


def _page_rotation(image_bytes: bytes) -> int:
    """How far the page must be turned clockwise to be upright; 0 if unsure, disabled or failing.

    AUTO_ROTATE=0 in .env switches the check off. A failure of this extra step must not cost the
    extraction, so the page is then simply used as it is.
    """
    if (os.getenv("AUTO_ROTATE") or "1").strip().lower() in ("0", "false", "no", "off"):
        return 0
    try:
        # Tesseract first: local and free. The model is only asked when it cannot tell.
        local = tesseract_rotation(image_bytes)
        if local is not None:
            return local
        return detect_rotation(client, _model_name(), image_bytes)
    except Exception as exc:
        logger.warning("orientation check skipped: %s", type(exc).__name__)
        return 0


def extract_with_agent_loop(image_bytes: bytes) -> dict:
    """Identify who sent the form, then extract its fields, zooming in with crop_image as needed.

    Two steps: classify_entity settles the sender first, and the page then goes to Claude with
    the prompt for that sender (the general prompt plus the sender's layout notes, if it has any;
    the general prompt alone if it was not identified). Returns the parsed JSON fields. Raises
    RuntimeError if Claude declines, is cut off, or doesn't finish within MAX_AGENT_TURNS.
    """
    # A sideways scan is turned upright for the model only; the stored page is left as it is, and
    # the regions recorded below are mapped back onto it.
    original_bytes = image_bytes
    rotation = _page_rotation(image_bytes)
    if rotation:
        logger.info("page rotated %d degrees clockwise for extraction", rotation)
        image_bytes = rotate_image(image_bytes, rotation)
    entity, evidence, entity_score = classify_entity(image_bytes)
    # The sender's name, not the page: nothing about the patient reaches the log.
    logger.info("sending entity: %s", entity or "not identified")
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
            {"type": "text", "text": build_extraction_prompt(entity)},
        ],
    }]
    # The sender was settled above, so the loop must not send Claude back to crop for it.
    cropped_fields: set[str] = {"sending_entity"}
    # Where on the page each field was read, as [x0, y0, x1, y1] page fractions - only from tight
    # crops, the same ones that count as having read the field. The review mode zooms to these.
    regions: dict[str, list[list[float]]] = {}
    reminders = 0
    # Barcodes are read once, from the page itself, as a second source for the central number.
    try:
        barcodes = decode_barcodes(original_bytes)
    except Exception as exc:  # a decoder failure must not cost the whole extraction
        logger.warning("barcode decoding skipped: %s", type(exc).__name__)
        barcodes = []
    for _ in range(MAX_AGENT_TURNS):
        response = client.messages.create(
            model=_model_name(),
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
            # The sender comes from the classification step, already checked against the closed list.
            _set_sending_entity(result, entity, evidence, entity_score)
            # A field scored CONF_EMPTY_BELOW or more is kept and flagged, not dropped (see the bands).
            _apply_confidence(result)
            # Hard validity checks in code (ID, date, sender, number lists): a failing field is
            # flagged and its confidence capped, whatever score the model gave it.
            validate_extraction(result, ok_above=CONF_OK_ABOVE)
            apply_barcode_check(result, barcodes)
            result["page_rotation"] = rotation
            result["regions"] = {f: [unrotate_box(b, rotation) for b in boxes] for f, boxes in regions.items()}
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
            claimed = [f for f in args.get("fields") or [] if f in CROP_FIELDS]
            if _crop_area(args) <= MAX_READ_CROP_AREA:
                cropped_fields.update(claimed)
                box = _crop_box(args)
                for field in claimed:
                    if box not in regions.setdefault(field, []):
                        regions[field].append(box)
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
