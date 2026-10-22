"""Tests for backend/app/services/calibration_store.py and the run model in main.py."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.main import CalibrationRun
from backend.app.services import calibration_store as store

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def run(run_id="run-0000-0001", reviewed=10, correct=9) -> dict:
    return {"run_id": run_id, "forms": 4, "reviewed": reviewed, "correct": correct, "ok_above": 70,
            "bands": [{"low": 0, "high": 9, "reviewed": 1, "correct": 0}], "fields": [], "suggestion": {"kind": "keep"}}


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_a_missing_file_is_an_empty_history(tmp_path):
    assert store.load_runs(tmp_path / "none.json") == []


def test_a_saved_run_is_written_with_the_server_time(tmp_path):
    path = tmp_path / "data" / "runs.json"  # the folder does not exist yet
    runs = store.save_run(path, run(), now=at(0))
    assert runs[0]["saved_at"] == "2026-10-22T08:00:00+00:00"
    assert json.loads(path.read_text(encoding="utf-8"))["runs"] == runs


def test_only_the_last_five_runs_are_kept_oldest_dropped_first(tmp_path):
    path = tmp_path / "runs.json"
    for i in range(7):
        store.save_run(path, run(run_id=f"run-0000-000{i}"), now=at(i))
    kept = [r["run_id"] for r in store.load_runs(path)]
    assert kept == [f"run-0000-000{i}" for i in range(2, 7)]


def test_saving_the_same_run_again_replaces_it_and_does_not_count_it_twice(tmp_path):
    path = tmp_path / "runs.json"
    store.save_run(path, run(reviewed=5, correct=5), now=at(0))
    store.save_run(path, run(reviewed=12, correct=11), now=at(30))
    runs = store.load_runs(path)
    assert len(runs) == 1
    assert (runs[0]["reviewed"], runs[0]["correct"]) == (12, 11)


def test_a_resaved_old_run_counts_as_the_newest(tmp_path):
    path = tmp_path / "runs.json"
    for i in range(5):
        store.save_run(path, run(run_id=f"run-0000-000{i}"), now=at(i))
    store.save_run(path, run(run_id="run-0000-0000"), now=at(60))
    assert [r["run_id"] for r in store.load_runs(path)][-1] == "run-0000-0000"
    assert len(store.load_runs(path)) == 5


@pytest.mark.parametrize("content", ["{not json", "[]", '{"runs": "x"}', '{"runs": [1, {"no_id": 1}]}', ""])
def test_a_damaged_file_starts_a_new_history_instead_of_failing(tmp_path, content):
    path = tmp_path / "runs.json"
    path.write_text(content, encoding="utf-8")
    assert store.load_runs(path) == []
    assert len(store.save_run(path, run(), now=at(0))) == 1


def test_no_temporary_file_is_left_behind(tmp_path):
    store.save_run(tmp_path / "runs.json", run(), now=at(0))
    assert [p.name for p in tmp_path.iterdir()] == ["runs.json"]


def payload(**over) -> dict:
    return {**run(), **over}


class TestRunModel:
    def test_a_normal_run_is_accepted(self):
        assert CalibrationRun(**payload()).run_id == "run-0000-0001"

    @pytest.mark.parametrize("run_id", ["short", "has space in it", "../../etc/passwd", "x" * 65, ""])
    def test_a_bad_run_id_is_rejected(self, run_id):
        with pytest.raises(ValidationError):
            CalibrationRun(**payload(run_id=run_id))

    def test_negative_counts_are_rejected(self):
        with pytest.raises(ValidationError):
            CalibrationRun(**payload(reviewed=-1))

    def test_oversized_lists_are_rejected(self):
        with pytest.raises(ValidationError):
            CalibrationRun(**payload(bands=[{"low": 0, "high": 9, "reviewed": 0, "correct": 0}] * 11))

    def test_a_form_value_has_no_place_in_the_saved_record(self):
        saved = CalibrationRun(**payload(id_number="123456782")).model_dump()
        assert "id_number" not in saved
