"""Tests for saving exports to SQLite and the key that guards reading them. All data is made up."""

import asyncio
from io import BytesIO

import msoffcrypto
import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from backend.app import main
from backend.app.services.db import load_rows, save_rows


def row(link="http://x/a.png", first="דנה"):
    return {"values": {"first_name": first, "id_number": "000000018"}, "link": link, "source": "a.pdf", "audit": []}


def test_saving_the_same_page_again_updates_its_row(tmp_path):
    db = tmp_path / "t.db"
    save_rows(db, [row(first="דנה")])
    save_rows(db, [row(first="רונית"), row(link="http://x/b.png")])
    rows = load_rows(db)
    assert len(rows) == 2
    first = next(r for r in rows if r["link"].endswith("a.png"))
    assert first["first_name"] == "רונית"
    assert first["id_number"] == "000000018"


def test_export_saves_to_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "t.db")
    body = main.ExportRequest(rows=[main.ExportRow(values={"first_name": "דנה", "id_number": "000000018"}, link="http://x/a.png")])
    response = asyncio.run(main.export(body))
    assert response.status_code == 200
    assert load_rows(tmp_path / "t.db")[0]["first_name"] == "דנה"


def test_reading_needs_the_configured_key(monkeypatch):
    monkeypatch.setenv("DB_API_KEY", "secret")
    main.require_api_key("secret")
    for bad in ("", "wrong"):
        with pytest.raises(HTTPException) as err:
            main.require_api_key(bad)
        assert err.value.status_code == 403


def test_no_configured_key_means_nobody_can_read(monkeypatch):
    monkeypatch.delenv("DB_API_KEY", raising=False)
    with pytest.raises(HTTPException):
        main.require_api_key("")


def test_excel_is_locked_with_the_password_and_unchanged_without_one():
    plain = main._build_workbook([main.ExportRow(values={"first_name": "דנה"}, link="http://x/a.png")])
    assert main._encrypt_workbook(plain, "") == plain
    locked = main._encrypt_workbook(plain, "pw")
    assert locked != plain
    out = BytesIO()
    file = msoffcrypto.OfficeFile(BytesIO(locked))
    file.load_key(password="pw")
    file.decrypt(out)
    assert load_workbook(out).active["A2"].value == "דנה"
