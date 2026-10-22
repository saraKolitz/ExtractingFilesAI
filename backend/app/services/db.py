import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

FIELD_KEYS = [
    "first_name",
    "last_name",
    "id_number",
    "central_numbers",
    "received_date",
    "sending_entity",
    "commitment_numbers",
]

# One row per commitment. The document link is unique per page, so exporting the same page
# again updates its row instead of adding a copy. All values are TEXT (an ID keeps its zeros).
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL,
    exported_at TEXT NOT NULL,
    {", ".join(f"{key} TEXT NOT NULL DEFAULT ''" for key in FIELD_KEYS)},
    audit TEXT NOT NULL DEFAULT '[]'
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def save_rows(db_path: Path, rows: list[dict]) -> str:
    """Upsert the exported rows in one transaction and return the batch id. Each row is
    {"values": {...}, "link": str, "source": str, "audit": [...]}. If anything fails nothing is
    written, so the caller can refuse the export."""
    batch_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    columns = ["link", "source", "batch_id", "exported_at", *FIELD_KEYS, "audit"]
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "link")
    sql = (
        f"INSERT INTO commitments ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
        f"ON CONFLICT(link) DO UPDATE SET {updates}"
    )
    with closing(_connect(db_path)) as conn, conn:
        for row in rows:
            values = row["values"]
            conn.execute(
                sql,
                [
                    row["link"],
                    row.get("source", ""),
                    batch_id,
                    now,
                    *(values.get(key, "") for key in FIELD_KEYS),
                    json.dumps(row.get("audit", []), ensure_ascii=False),
                ],
            )
    return batch_id


def load_rows(db_path: Path) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        found = conn.execute("SELECT * FROM commitments ORDER BY id").fetchall()
    return [{**dict(r), "audit": json.loads(r["audit"])} for r in found]
