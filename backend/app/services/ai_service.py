import itertools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
# The API is configured at import time, before main.py's load_dotenv() call can run.
load_dotenv(ROOT / ".env")

# Logs must never contain personal data (file names, content) - only error types.
logger = logging.getLogger("uvicorn.error")

DEFAULT_MODEL = "gemini-2.5-flash"
# extract_data_from_image returns its errors as text starting with this prefix.
ERROR_PREFIX = "Error: "
# Free-tier quota (e.g. 5 requests/minute) is shared across the whole project, and one
# extraction can cost 2 requests when the model calls the get_entity_hints tool (one request to
# get the tool call, one more to send back its result). On a 429, Google's response already says
# exactly how long to wait - retry a few times honoring that instead of failing outright.
MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 20.0

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

    Call this exactly once, right after you have identified sending_entity, passing that same
    value so you can check for entity-specific notes (e.g. where the commitment number is
    labeled, or a field this entity's forms never have) before extracting the remaining fields.

    Args:
        entity_name: The sending entity's name, copied exactly as one of the values in the
            closed list given in the main instructions.

    Returns:
        The recorded layout hints for that entity, or an empty string if none are recorded or
        the name isn't recognized.
    """
    return ENTITY_TEMPLATES.get(entity_name, {}).get("hints", "")


PROMPT = """You are an expert AI data extraction system specialized in scanning Hebrew administrative forms.
Your task is to extract specific data fields from the provided image and return ONLY a valid, structured JSON object. Do not include any Markdown formatting, explanations, or conversational text outside the JSON.

### EXTRACTION RULES:
1. STRICT ACCURACY: Do NOT guess or invent information. If a field is not present or cannot be read, return `null` for strings or an empty array `[]` for lists.
2. UNCERTAINTY: If you extract a value but are not 100% confident in your reading (e.g., due to unclear handwriting), add the exact JSON key of that field to the `fields_requiring_review` array.
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
   office, an authority or foreign payer (e.g. "הרשות הפלסטינאית"), an insurance company, a private
   clinic or an external laboratory. Such a body is NOT on the closed list, and there is no
   listed value that stands for it: leave sending_entity null and add it to
   `fields_requiring_review`. Never substitute a listed HMO because it is the most common one,
   because its name partly resembles what is written, or because a value "has to" be filled in.
5. If you still cannot confidently determine the issuing institution, leave sending_entity null
   and add it to `fields_requiring_review` - do not call the hints tool in that case.

Once you have a confident match, call the `get_entity_hints` tool with that exact entity name to
retrieve any known layout notes for it, and use what it returns (if anything) while extracting
the remaining fields below.

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
Once you are done calling tools and have everything you need, respond with ONLY this JSON object
(no Markdown, no explanation):
{
  "first_name": null,
  "last_name": null,
  "id_number": null,
  "central_numbers": [],
  "received_date": null,
  "sending_entity": null,
  "sending_entity_evidence": null,
  "commitment_numbers": [],
  "fields_requiring_review": []
}
"""
# ENTITY_TEMPLATES is the single source of truth for the closed list of sending entities;
# the prompt is built from it so the list can't drift out of sync with entity_templates.json.
PROMPT = PROMPT.replace("<<ENTITY_LIST>>", ", ".join(ENTITY_TEMPLATES.keys()))

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
# The JSON object inside the model's reply, even if it arrives wrapped in a Markdown fence.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _evidence_names(entity: str, evidence) -> bool:
    """True if the wording the model copied off the page actually names this entity."""
    if not isinstance(evidence, str):
        return False
    quoted = evidence.casefold()
    return any(alias in quoted for alias in _ENTITY_ALIASES[entity])


def _enforce_closed_list(text: str) -> str:
    """Enforces the closed list of sending entities (data model field 6) in code.

    The prompt forbids a value outside the list, but faced with a sender that simply isn’t on it
    (a foreign payer, a government authority, a private lab) the model still tends to fill in the
    closest-looking HMO rather than leave the field empty - and a plausible wrong name is worse
    than an empty one, since nothing marks it as invented. A known alias is mapped to its
    canonical name; a value that is off the list, or that the model could not point to anywhere on
    the page, is cleared and flagged for review.
    """
    match = _JSON_RE.search(text)
    if not match:
        return text
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text

    changed = False
    entity = data.get("sending_entity")
    if isinstance(entity, str) and entity.strip():
        canonical = _ALIAS_TO_ENTITY.get(entity.strip().casefold())
        # A name on the list is not enough: the model also hallucinates listed names (a screenshot
        # with no letterhead at all came back as "מכבי"). The quote it had to copy from the page
        # must actually contain the entity’s name, or the classification had nothing behind it.
        if canonical is not None and not _evidence_names(canonical, data.get("sending_entity_evidence")):
            canonical = None
        if canonical != entity:
            data["sending_entity"] = canonical
            changed = True
            if canonical is None:
                _flag(data, "sending_entity")
                logger.info("sending_entity cleared: not on the closed list, or not shown on the page")

    # sending_entity_evidence only exists to ground the classification above - it is not one of
    # the extracted fields, so it has no business in the reviewer’s list of fields to check.
    flags = data.get("fields_requiring_review")
    if isinstance(flags, list) and "sending_entity_evidence" in flags:
        data["fields_requiring_review"] = [f for f in flags if f != "sending_entity_evidence"]
        changed = True

    return json.dumps(data, ensure_ascii=False, indent=2) if changed else text


def _flag(data: dict, field: str) -> None:
    """Adds a field to fields_requiring_review, repairing the key if the model dropped it."""
    flags = data.get("fields_requiring_review")
    if not isinstance(flags, list):
        flags = []
        data["fields_requiring_review"] = flags
    if field not in flags:
        flags.append(field)


# API_KEY may hold several comma-separated keys, so quota can be spread across them (useful on
# the free tier, where quota is per-key). One client per key, built once at import time.
API_KEYS = [k.strip() for k in (os.getenv("API_KEY") or "").split(",") if k.strip()]
_clients = [genai.Client(api_key=k) for k in API_KEYS] or [genai.Client(api_key=None)]
# A shared round-robin starting point so concurrent requests spread across keys instead of all
# starting from the same one; protected by a lock since multiple extractions can run at once
# (FastAPI's run_in_threadpool).
_start_index_lock = threading.Lock()
_start_index_cycle = itertools.cycle(range(len(_clients)))


def _next_start_index() -> int:
    with _start_index_lock:
        return next(_start_index_cycle)


def _retry_delay_seconds(exc: errors.APIError) -> float:
    """Reads the exact wait time Google's 429 response recommends (google.rpc.RetryInfo),
    falling back to a fixed delay if that detail isn't present."""
    try:
        for detail in exc.details.get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                return float(detail["retryDelay"].rstrip("s"))
    except (AttributeError, KeyError, ValueError):
        pass
    return DEFAULT_RETRY_DELAY_SECONDS


def extract_data_from_image(image_path: str) -> str:
    try:
        model = os.getenv("MODEL_NAME") or DEFAULT_MODEL  # "or": an empty .env line must fall back too.
        start = _next_start_index()
        with Image.open(image_path) as image:
            last_exc: errors.APIError | None = None
            for pass_num in range(MAX_RATE_LIMIT_RETRIES + 1):
                # Within a pass, try every key once, rotating immediately on a retryable error -
                # quota (429) is per-key, so a key that's out of quota isn't worth waiting on
                # while others may still have room. Only once ALL keys fail in the same pass do
                # we sleep.
                for offset in range(len(_clients)):
                    idx = (start + offset) % len(_clients)
                    # A fresh chat per attempt: a retry can land mid-automatic-function-calling
                    # (after the tool call already went into this chat's history), so retrying on
                    # the same chat object would resend a partially-completed turn instead of a
                    # clean one.
                    chat = _clients[idx].chats.create(
                        model=model,
                        config=types.GenerateContentConfig(tools=[get_entity_hints]),
                    )
                    try:
                        # One chat session, one round trip from our side: the model classifies
                        # the sending entity itself, calls get_entity_hints as a tool
                        # mid-generation to fetch that entity's layout notes, and then finishes
                        # extracting the rest of the fields - all handled by the SDK's automatic
                        # function calling (requires google-genai, not the deprecated
                        # google-generativeai, to correctly carry the thought_signature that
                        # Gemini 3's thinking models require across a tool-call turn).
                        return _enforce_closed_list(chat.send_message([PROMPT, image]).text or "")
                    except (errors.ClientError, errors.ServerError) as exc:
                        # 429 (quota) is per-key, worth rotating off immediately. 5xx (e.g. 503
                        # "model overloaded") isn't key-specific - rotating doesn't fix it, but
                        # it's just as transient, so it gets the same retry-with-backoff treatment
                        # rather than failing outright on the first busy moment.
                        if exc.code != 429 and exc.code < 500:
                            raise
                        last_exc = exc
                        logger.error("key %d failed (%s), trying next key", idx, exc.code)
                if pass_num < MAX_RATE_LIMIT_RETRIES:
                    logger.error("all keys failed, backing off (pass %d)", pass_num + 1)
                    time.sleep(_retry_delay_seconds(last_exc))
            raise last_exc
    except Exception as exc:
        logger.error("image extraction failed: %s", type(exc).__name__)
        return f"{ERROR_PREFIX}{exc}"
