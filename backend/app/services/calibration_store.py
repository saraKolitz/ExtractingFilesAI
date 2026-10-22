"""The calibration history: a small JSON file with the last few runs.

A run is one working session, saved when its Excel file is exported. The file holds only counts
(how many fields were checked, how many the model got right, per score band and per field) - never
a name, an ID number or any value from a form - so it can sit next to the code without being
personal data. Saving the same run again replaces it, so exporting twice does not count it twice.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_RUNS = 5

logger = logging.getLogger("uvicorn.error")
# Requests are served on worker threads; two saves at once must not lose one of the runs.
_lock = threading.Lock()


def load_runs(path: Path) -> list[dict]:
    """The saved runs, oldest first. A missing, unreadable or malformed file is an empty history."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.error("calibration history is unreadable - starting a new one")
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict) and isinstance(r.get("run_id"), str)]


def save_run(path: Path, run: dict, now: datetime | None = None) -> list[dict]:
    """Adds the run (or replaces the one with the same run_id), keeps the newest MAX_RUNS, and
    returns what is now in the file. The time is set here, not taken from the client."""
    record = {**run, "saved_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    with _lock:
        runs = [r for r in load_runs(path) if r["run_id"] != record["run_id"]]
        runs.append(record)
        runs.sort(key=lambda r: str(r.get("saved_at", "")))
        runs = runs[-MAX_RUNS:]
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary file and moved into place, so a crash never leaves half a file.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return runs
