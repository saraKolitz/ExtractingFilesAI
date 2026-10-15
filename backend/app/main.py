import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .services.ai_service import ERROR_PREFIX, extract_data_from_image
from .services.processor import UPLOADS_DIR, PageInfo, _process

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# A file id is uuid4().hex (see processor._process). It becomes part of a file path, so it must
# be exactly that and nothing else.
FILE_ID_RE = re.compile(r"[0-9a-f]{32}")
MAX_ERROR_DETAIL = 300

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

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


class ExtractResponse(BaseModel):
    text: str


@app.post("/extract/{file_id}/{page_number}", response_model=ExtractResponse)
async def extract(file_id: str, page_number: int):
    if not FILE_ID_RE.fullmatch(file_id):
        raise HTTPException(status_code=400, detail="invalid file id")
    image_path = UPLOADS_DIR / file_id / "pages" / f"page_{page_number}.png"
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="page not found")
    text = await run_in_threadpool(extract_data_from_image, str(image_path))
    if text.startswith(ERROR_PREFIX):
        raise HTTPException(status_code=502, detail=text[len(ERROR_PREFIX):][:MAX_ERROR_DETAIL])
    return ExtractResponse(text=text)


@app.get("/health")
def health():
    return {"status": "ok"}
