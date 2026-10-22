"""Tests for the routes in backend/app/main.py: everything lives under /api (Vercel hands the service
the original path, so the prefix is part of the route), and the page-image links use the public
origin. No model is called - the extract route is only exercised on requests that fail before it.

Uploads go to a temporary folder, never to the real uploads/. Run from the project root:
backend\\venv\\Scripts\\python.exe -m pytest
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest

# UPLOADS_DIR is read when the processor module is first imported, so it is set before anything
# imports backend.app.main.
_UPLOADS = tempfile.mkdtemp(prefix="uploads-test-")
os.environ["UPLOADS_DIR"] = _UPLOADS

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import API_PREFIX, app  # noqa: E402
from backend.app.services import processor  # noqa: E402

if Path(processor.UPLOADS_DIR) != Path(_UPLOADS):  # another module imported the processor first
    pytest.skip("UPLOADS_DIR was already fixed by an earlier import - refusing to write into it", allow_module_level=True)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def remove_uploads():
    yield
    shutil.rmtree(_UPLOADS, ignore_errors=True)


def png_bytes() -> bytes:
    ok, encoded = cv2.imencode(".png", np.full((120, 80, 3), 255, np.uint8))
    assert ok
    return encoded.tobytes()


def upload(**headers):
    return client.post(f"{API_PREFIX}/upload", files={"files": ("form.png", png_bytes(), "image/png")}, headers=headers)


# --- the routes carry the /api prefix ---------------------------------------------------------


def test_the_prefix_is_api():
    assert API_PREFIX == "/api"


def test_health_is_under_api():
    response = client.get("/api/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("method, path", [("get", "/health"), ("post", "/upload"), ("post", "/export"), ("post", "/extract/" + "a" * 32 + "/1")])
def test_the_old_unprefixed_paths_are_gone(method, path):
    assert getattr(client, method)(path).status_code in (404, 405)


def test_an_invalid_file_id_is_refused_before_anything_runs():
    assert client.post("/api/extract/not-an-id/1").status_code == 400


def test_a_page_that_was_never_uploaded_is_not_found():
    response = client.post(f"/api/extract/{'a' * 32}/1")
    assert response.status_code == 404 and response.json()["detail"] == "page not found"


def test_export_builds_a_workbook():
    body = {"rows": [{"values": {"first_name": "דנה", "commitment_numbers": "555"}, "link": "https://x.example/api/files/a"}]}
    response = client.post("/api/export", json=body)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert response.content[:2] == b"PK"  # an .xlsx is a zip


# --- the page links ---------------------------------------------------------------------------


def test_upload_returns_links_under_api_files_that_can_be_fetched():
    response = upload()
    assert response.status_code == 200 and response.json()["errors"] == []
    page = response.json()["pages"][0]
    assert re.fullmatch(r"http://testserver/api/files/[0-9a-f]{32}/pages/page_1\.png", page["image_url"])
    assert re.fullmatch(r"http://testserver/api/files/[0-9a-f]{32}/pages/thumb_1\.jpg", page["thumbnail_url"])
    for url in (page["image_url"], page["thumbnail_url"]):
        served = client.get(url.removeprefix("http://testserver"))
        assert served.status_code == 200 and served.content


def test_links_use_the_host_and_scheme_the_browser_used_behind_a_proxy():
    page = upload(**{"x-forwarded-proto": "https", "x-forwarded-host": "forms.example.com"}).json()["pages"][0]
    assert page["image_url"].startswith("https://forms.example.com/api/files/")
    assert page["thumbnail_url"].startswith("https://forms.example.com/api/files/")


def test_a_forwarded_scheme_that_is_not_http_or_https_is_ignored():
    page = upload(**{"x-forwarded-proto": "javascript"}).json()["pages"][0]
    assert page["image_url"].startswith("http://testserver/api/files/")


def test_the_first_value_of_a_forwarded_list_is_used():
    page = upload(**{"x-forwarded-proto": "https, http", "x-forwarded-host": "a.example.com, internal"}).json()["pages"][0]
    assert page["image_url"].startswith("https://a.example.com/api/files/")


# --- where uploads are kept -------------------------------------------------------------------


def uploads_dir_in_a_fresh_process(**env) -> str:
    """processor.UPLOADS_DIR as a new process sees it.

    VERCEL and UPLOADS_DIR are taken from env only (UPLOADS_DIR empty when not given - it must be
    set, even to "", because the processor loads the developer's own .env and a variable that is
    already set is not overridden by it).
    """
    clean = {k: v for k, v in os.environ.items() if k not in ("UPLOADS_DIR", "VERCEL")}
    code = "from backend.app.services import processor; print(processor.UPLOADS_DIR.as_posix())"
    result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                            env={**clean, "UPLOADS_DIR": "", **env, "PYTHONPATH": str(PROJECT_ROOT)},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-500:]
    return result.stdout.strip().splitlines()[-1]


def test_uploads_default_to_the_project_folder_locally():
    assert uploads_dir_in_a_fresh_process() == (PROJECT_ROOT / "uploads").as_posix()


def test_uploads_go_to_tmp_on_vercel_where_the_project_is_read_only():
    assert uploads_dir_in_a_fresh_process(VERCEL="1") == "/tmp/uploads"


def test_an_explicit_uploads_dir_wins_everywhere():
    assert uploads_dir_in_a_fresh_process(VERCEL="1", UPLOADS_DIR="/data/forms") == "/data/forms"


def test_an_empty_uploads_dir_line_falls_back_instead_of_becoming_the_current_directory():
    assert uploads_dir_in_a_fresh_process(UPLOADS_DIR="") == (PROJECT_ROOT / "uploads").as_posix()
