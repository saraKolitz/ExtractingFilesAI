import logging
import os
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Logs must never contain personal data (file names, content) - only ids and counts.
logger = logging.getLogger("uvicorn.error")

ROOT = Path(__file__).resolve().parents[2]
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", ROOT / "uploads"))
RENDER_DPI = 200
THUMB_WIDTH = 360
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

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


class PageInfo(BaseModel):
    file_id: str
    file_name: str
    page_number: int
    image_url: str
    thumbnail_url: str
    width: int
    height: int


class UploadError(BaseModel):
    file_name: str
    detail: str


class UploadResponse(BaseModel):
    pages: list[PageInfo]
    errors: list[UploadError]


def _process(file: UploadFile, data: bytes, base_url: str) -> list[PageInfo]:
    name = file.filename or "file"
    ext = Path(name).suffix.lower()
    if ext not in PDF_EXTS | IMAGE_EXTS:
        raise ValueError("unsupported file type")

    file_id = uuid.uuid4().hex
    folder = UPLOADS_DIR / file_id
    pages_dir = folder / "pages"
    pages_dir.mkdir(parents=True)
    (folder / f"original{ext}").write_bytes(data)

    try:
        doc = fitz.open(stream=data, filetype="pdf" if ext in PDF_EXTS else None)
    except Exception as exc:
        raise ValueError("file could not be opened") from exc

    pages: list[PageInfo] = []
    with doc:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=RENDER_DPI, colorspace=fitz.csRGB, alpha=False)
            pix.save(pages_dir / f"page_{index}.png")

            scale = THUMB_WIDTH / page.rect.width
            thumb = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            thumb.save(pages_dir / f"thumb_{index}.jpg", jpg_quality=80)

            prefix = f"{base_url}/files/{file_id}/pages"
            pages.append(
                PageInfo(
                    file_id=file_id,
                    file_name=name,
                    page_number=index,
                    image_url=f"{prefix}/page_{index}.png",
                    thumbnail_url=f"{prefix}/thumb_{index}.jpg",
                    width=pix.width,
                    height=pix.height,
                )
            )
    logger.info("stored file %s with %d page(s)", file_id, len(pages))
    return pages


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


@app.get("/health")
def health():
    return {"status": "ok"}
