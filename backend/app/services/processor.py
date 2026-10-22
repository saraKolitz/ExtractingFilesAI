import logging
import os
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import UploadFile
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[3]
# UPLOADS_DIR is read at import time, and main.py imports this module before its own
# load_dotenv() call runs - so the .env file has to be loaded here too.
load_dotenv(ROOT / ".env")

# Logs must never contain personal data (file names, content) - only ids and counts.
logger = logging.getLogger("uvicorn.error")

# On Vercel the project's files are read-only and only /tmp can be written to - and that is private
# to one function instance and short-lived, so it is not a place to keep uploads between requests
# (see the deployment notes). "or" and not a getenv default: an empty UPLOADS_DIR= line in .env
# must fall back too, or it would become Path("") - the current directory.
_DEFAULT_UPLOADS_DIR = Path("/tmp/uploads") if os.getenv("VERCEL") else ROOT / "uploads"
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR") or _DEFAULT_UPLOADS_DIR)
RENDER_DPI = 200
THUMB_WIDTH = 360
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


class PageInfo(BaseModel):
    file_id: str
    file_name: str
    page_number: int
    image_url: str
    thumbnail_url: str
    width: int
    height: int


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
