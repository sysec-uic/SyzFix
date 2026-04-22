"""Data persistence with SQLite progress tracking and JSON storage."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

from . import config
from .models import BugEntry

logger = logging.getLogger(__name__)


class ProgressDB:
    """SQLite-based progress tracker for resumable processing."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS bugs (
                bug_id TEXT PRIMARY KEY,
                title TEXT,
                step TEXT DEFAULT 'pending',
                -- steps: pending, syzbot_fetched, patches_fetched,
                --        discussions_fetched, patchwork_fetched, processed, exported
                errors TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bug_list (
                bug_id TEXT PRIMARY KEY,
                title TEXT,
                link TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_bugs_step ON bugs(step);
        """)
        self._conn.commit()

    def save_bug_list(self, bugs: list[dict]):
        """Save the full list of bugs from syzbot."""
        self._conn.executemany(
            "INSERT OR IGNORE INTO bug_list (bug_id, title, link) VALUES (?, ?, ?)",
            [(b["bug_id"], b["title"], b["link"]) for b in bugs],
        )
        # Also create pending entries in bugs table
        self._conn.executemany(
            "INSERT OR IGNORE INTO bugs (bug_id, title) VALUES (?, ?)",
            [(b["bug_id"], b["title"]) for b in bugs],
        )
        self._conn.commit()

    def get_bugs_at_step(self, step: str) -> list[str]:
        """Get bug IDs at a specific processing step."""
        rows = self._conn.execute(
            "SELECT bug_id FROM bugs WHERE step = ?", (step,)
        ).fetchall()
        return [r["bug_id"] for r in rows]

    def get_all_bug_ids(self) -> list[str]:
        """Get all bug IDs."""
        rows = self._conn.execute("SELECT bug_id FROM bugs").fetchall()
        return [r["bug_id"] for r in rows]

    def get_pending_bugs(self, target_step: str) -> list[str]:
        """Get bugs that haven't reached the target step yet."""
        step_order = [
            "pending",
            "syzbot_fetched",
            "patches_fetched",
            "discussions_fetched",
            "patchwork_fetched",
            "processed",
            "exported",
        ]
        target_idx = step_order.index(target_step)
        pending_steps = step_order[:target_idx]
        placeholders = ",".join("?" * len(pending_steps))
        rows = self._conn.execute(
            f"SELECT bug_id FROM bugs WHERE step IN ({placeholders})",
            pending_steps,
        ).fetchall()
        return [r["bug_id"] for r in rows]

    def update_step(self, bug_id: str, step: str, errors: list[str] | None = None):
        """Update the processing step for a bug."""
        if errors:
            self._conn.execute(
                "UPDATE bugs SET step = ?, errors = ?, updated_at = CURRENT_TIMESTAMP WHERE bug_id = ?",
                (step, json.dumps(errors), bug_id),
            )
        else:
            self._conn.execute(
                "UPDATE bugs SET step = ?, updated_at = CURRENT_TIMESTAMP WHERE bug_id = ?",
                (step, bug_id),
            )
        self._conn.commit()

    def add_error(self, bug_id: str, error: str):
        """Append an error to a bug's error list."""
        row = self._conn.execute(
            "SELECT errors FROM bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        if row:
            errors = json.loads(row["errors"])
            errors.append(error)
            self._conn.execute(
                "UPDATE bugs SET errors = ? WHERE bug_id = ?",
                (json.dumps(errors), bug_id),
            )
            self._conn.commit()

    def get_stats(self) -> dict[str, int]:
        """Get processing statistics."""
        rows = self._conn.execute(
            "SELECT step, COUNT(*) as cnt FROM bugs GROUP BY step"
        ).fetchall()
        return {r["step"]: r["cnt"] for r in rows}

    def close(self):
        self._conn.close()


class DataStore:
    """JSON file-based storage for bug data."""

    def __init__(self):
        config.RAW_SYZBOT_DIR.mkdir(parents=True, exist_ok=True)
        config.RAW_PATCHES_DIR.mkdir(parents=True, exist_ok=True)
        config.RAW_DISCUSSIONS_DIR.mkdir(parents=True, exist_ok=True)
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    def save_raw_syzbot(self, bug_id: str, data: dict):
        """Save raw syzbot API response."""
        path = config.RAW_SYZBOT_DIR / f"{bug_id}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_raw_syzbot(self, bug_id: str) -> dict | None:
        """Load raw syzbot API response."""
        path = config.RAW_SYZBOT_DIR / f"{bug_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def save_processed(self, bug: BugEntry):
        """Save processed bug data."""
        path = config.PROCESSED_DIR / f"{bug.bug_id}.json"
        path.write_text(
            json.dumps(asdict(bug), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def load_processed(self, bug_id: str) -> dict | None:
        """Load processed bug data."""
        path = config.PROCESSED_DIR / f"{bug_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def save_dataset_entry(self, entry: dict, bug_id: str):
        """Save a single dataset entry."""
        config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
        path = config.DATASET_DIR / f"{bug_id}.json"
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
