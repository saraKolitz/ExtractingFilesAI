import hmac
import json
import os
import re
import sqlite3
from io import BytesIO
from pathlib import Path

import msoffcrypto
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from pydantic import BaseModel, Field

# from .services.ai_service import ERROR_PREFIX, extract_data_from_image
# from .services.ai_service import extract_fields_with_gemini
# from .services.ai_service import extract_full_image_with_claude
from .services.ai_service import extract_with_agent_loop
from .services.calibration_store import load_runs, save_run
from .services.db import load_rows, save_rows
# from .services.image_processing import extract_field_images
# from .services.ocr_service import get_word_coordinates
from .services.processor import UPLOADS_DIR, PageInfo, _process
from .services.validation import id_blocks_saving

# .env lives in the project root (CLAUDE.md section 8), one level above backend/.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# A file id is uuid4().hex (see processor._process). It becomes part of a file path, so it must
# be exactly that and nothing else.
FILE_ID_RE = re.compile(r"[0-9a-f]{32}")
MAX_ERROR_DETAIL = 300

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
# The last few calibration runs (counts only, no personal data); see calibration_store.py.
CALIBRATION_FILE = Path(os.getenv("CALIBRATION_FILE") or Path(__file__).resolve().parents[2] / "data" / "calibration_runs.json")

# Every export is also saved here (SQLite); see services/db.py. Under data/, which git ignores.
DB_FILE = Path(os.getenv("DB_FILE") or Path(__file__).resolve().parents[2] / "data" / "commitments.db")

app = FastAPI(title="Commitment Forms Extraction")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Layout: uploads/<file_id>/original.<ext>, uploads/<file_id>/pages/page_<n>.png, thumb_<n>.jpg
app.mount("/files", StaticFiles(directory=UPLOADS_DIR), name="files")


class UploadError(BaseModel):
    file_name: str
    detail: str


class UploadResponse(BaseModel):
    pages: list[PageInfo]
    errors: list[UploadError]


@app.post("/upload", response_model=UploadResponse)
async def upload(request: Request, request_files: list[UploadFile] = File(..., alias="files")):
    base_url = str(request.base_url).rstrip("/")
    if not request_files:
        raise HTTPException(status_code=400, detail="no files")
    pages: list[PageInfo] = []
    errors: list[UploadError] = []
    for f in request_files:
        try:
            pages.extend(await run_in_threadpool(_process, f, await f.read(), base_url))
        except ValueError as exc:
            errors.append(UploadError(file_name=f.filename or "file", detail=str(exc)))
    return UploadResponse(pages=pages, errors=errors)


# def _extract_fields(image_bytes: bytes) -> dict:
#     words_data = get_word_coordinates(image_bytes)
#     cropped_images = extract_field_images(image_bytes, words_data)
#     return extract_fields_with_gemini(cropped_images)


@app.post("/extract/{file_id}/{page_number}")
async def extract(file_id: str, page_number: int):
    if not FILE_ID_RE.fullmatch(file_id):
        raise HTTPException(status_code=400, detail="invalid file id")
    image_path = UPLOADS_DIR / file_id / "pages" / f"page_{page_number}.png"
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="page not found")
    image_bytes = image_path.read_bytes()
    # # Vision, OpenCV and Gemini calls all block, so the whole sequence runs off the event loop.
    # final_results = await run_in_threadpool(_extract_fields, image_bytes)
    # return final_results
    # The full page goes straight to Claude - no OCR or cropping. The call blocks, so run it off
    # the event loop. {"text": ...} is the shape the frontend reads (lib/api.ts).
    # text = await run_in_threadpool(extract_full_image_with_claude, image_bytes)
    # return {"text": text}
    # Claude may zoom in with crop_image over several turns before it answers.
    result = await run_in_threadpool(extract_with_agent_loop, image_bytes)
    # The frontend shows "text" as a string, so the fields go back as readable JSON text.
    return {"text": json.dumps(result, ensure_ascii=False, indent=2)}


EXPORT_COLUMNS = [
    ("first_name", "שם פרטי"),
    ("last_name", "שם משפחה"),
    ("id_number", "תעודת זהות"),
    ("central_numbers", "מספר מרכזי"),
    ("received_date", "תאריך קבלה"),
    ("sending_entity", "גורם שולח"),
    ("commitment_numbers", "מספר התחייבות"),
]
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class AuditEntry(BaseModel):
    field: str
    original: str
    final: str
    confidence: int | None = None
    status: str


class ExportRow(BaseModel):
    values: dict[str, str]
    link: str
    # The audit trail: where the row sits on screen, its source page, and each field's journey.
    number: int = 0
    source: str = ""
    audit: list[AuditEntry] = []


class ExportRequest(BaseModel):
    rows: list[ExportRow]


AUDIT_HEADERS = ["שורה", "מקור", "שדה", "ערך שחולץ", "ערך סופי", "ציון ביטחון", "סטטוס"]
HUMAN_CORRECTED = "תוקן ידנית"


def _add_audit_sheet(wb: Workbook, rows: list[ExportRow]) -> None:
    """A second sheet with every field's journey: what the model extracted, what was saved, and
    whether a person (or a barcode) vouched for it. Corrections are shaded, so the changes a
    reviewer made stand out from what the system did on its own."""
    ws = wb.create_sheet("יומן שינויים")
    ws.sheet_view.rightToLeft = True
    ws.append(AUDIT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    corrected = PatternFill("solid", fgColor="FDE9D2")
    for row in rows:
        for entry in row.audit:
            ws.append([row.number, row.source, entry.field, entry.original, entry.final, entry.confidence, entry.status])
            r = ws.max_row
            # Text stays text: a value starting with "=" must not turn into a formula.
            for col in (2, 3, 4, 5, 7):
                ws.cell(row=r, column=col).data_type = "s"
            if entry.status == HUMAN_CORRECTED:
                for c in ws[r]:
                    c.fill = corrected
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 22


def _build_workbook(rows: list[ExportRow]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True
    ws.append([label for _, label in EXPORT_COLUMNS] + ["קישור למסמך"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.values.get(key, "") for key, _ in EXPORT_COLUMNS] + [""])
        r = ws.max_row
        # Every value is text (an ID number must keep its leading zeros), and a string that
        # starts with "=" must not turn into a formula.
        for c in ws[r]:
            c.data_type = "s"
        link = ws.cell(row=r, column=len(EXPORT_COLUMNS) + 1)
        link.value = "פתיחת המסמך"
        link.hyperlink = row.link
        link.font = Font(color="0563C1", underline="single")
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 18
    if any(row.audit for row in rows):
        _add_audit_sheet(wb, rows)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _encrypt_workbook(content: bytes, password: str) -> bytes:
    """Lock the file so Excel asks for the password on open. No password configured: unchanged."""
    if not password:
        return content
    out = BytesIO()
    msoffcrypto.OfficeFile(BytesIO(content)).encrypt(password, out)
    return out.getvalue()


@app.post("/export")
async def export(body: ExportRequest):
    # The screen already refuses these; this is the same rule for a client that skips the screen.
    bad = [i for i, row in enumerate(body.rows, start=1) if id_blocks_saving(row.values.get("id_number", ""))]
    if bad:
        raise HTTPException(status_code=422, detail=f"id_number must be exactly 9 digits (rows {bad})")
    # Save first: if the database write fails, the export fails too, so the two never disagree.
    try:
        await run_in_threadpool(save_rows, DB_FILE, [row.model_dump() for row in body.rows])
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="could not save to the database")
    content = await run_in_threadpool(_build_workbook, body.rows)
    content = _encrypt_workbook(content, os.getenv("EXCEL_PASSWORD", ""))
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="commitments.xlsx"'},
    )


def require_api_key(x_api_key: str = Header(default="")):
    """Reading the database needs the key from DB_API_KEY. With no key configured nobody can read."""
    expected = os.getenv("DB_API_KEY", "")
    if not expected or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/records", dependencies=[Depends(require_api_key)])
async def get_records():
    return {"records": await run_in_threadpool(load_rows, DB_FILE)}


class CalibrationBand(BaseModel):
    low: int = Field(ge=0, le=100)
    high: int = Field(ge=0, le=100)
    reviewed: int = Field(ge=0)
    correct: int = Field(ge=0)


class CalibrationField(BaseModel):
    field: str = Field(max_length=40)
    reviewed: int = Field(ge=0)
    correct: int = Field(ge=0)


class CalibrationRun(BaseModel):
    # One id per working session, so exporting twice updates the run instead of adding another.
    run_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")
    forms: int = Field(ge=0)
    reviewed: int = Field(ge=0)
    correct: int = Field(ge=0)
    ok_above: int = Field(ge=0, le=100)
    bands: list[CalibrationBand] = Field(max_length=10)
    fields: list[CalibrationField] = Field(max_length=20)
    suggestion: dict[str, str | int | float | None] = Field(default_factory=dict, max_length=12)


@app.post("/calibration/runs")
async def save_calibration_run(run: CalibrationRun):
    runs = await run_in_threadpool(save_run, CALIBRATION_FILE, run.model_dump())
    return {"runs": runs}


@app.get("/calibration/runs")
async def get_calibration_runs():
    return {"runs": await run_in_threadpool(load_runs, CALIBRATION_FILE)}


@app.get("/health")
def health():
    return {"status": "ok"}
