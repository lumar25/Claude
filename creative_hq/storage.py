"""Persistent storage (SQLite): runs, tasks, sources, content history, spend."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    request TEXT NOT NULL,
    status TEXT NOT NULL,              -- running | failed | pending_approval | approved | rejected
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deliverable_dir TEXT,
    error TEXT,
    approval_note TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,               -- e.g. <run>-T3
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    depends_on TEXT NOT NULL,          -- JSON list of task ids
    status TEXT NOT NULL,              -- pending | running | completed | failed | skipped
    input_json TEXT,
    output_json TEXT,
    cost_usd REAL DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    session_id TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,          -- S1, S2 ... within the run
    url TEXT NOT NULL,
    title TEXT,
    publisher TEXT,
    accessed_via TEXT,                 -- WebSearch | WebFetch (as verified by the system)
    excerpt TEXT,
    verified INTEGER NOT NULL,         -- 1 = URL seen in real tool results
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    format TEXT NOT NULL,
    topic TEXT NOT NULL,
    angle TEXT,
    status TEXT NOT NULL,              -- pending_approval | approved | rejected
    path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    task_id TEXT,
    agent_id TEXT,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    # runs
    def create_run(self, run_id: str, client_id: str, request: str) -> None:
        self._exec(
            "INSERT INTO runs (id, client_id, request, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (run_id, client_id, request, "running", now(), now()),
        )

    def update_run(self, run_id: str, **fields) -> None:
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    # tasks
    def create_task(self, task_id: str, run_id: str, seq: int, agent_id: str, title: str, depends_on: list[str]) -> None:
        self._exec(
            "INSERT INTO tasks (id, run_id, seq, agent_id, title, depends_on, status) VALUES (?,?,?,?,?,?,?)",
            (task_id, run_id, seq, agent_id, title, json.dumps(depends_on), "pending"),
        )

    def update_task(self, task_id: str, **fields) -> None:
        for k in ("input_json", "output_json"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()

    # sources
    def add_source(self, run_id: str, client_id: str, src: dict, verified: bool) -> None:
        self._exec(
            "INSERT INTO sources (run_id, client_id, source_ref, url, title, publisher, accessed_via, excerpt, verified, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, client_id, src["id"], src["url"], src.get("title"), src.get("publisher"),
             src.get("accessed_via"), src.get("relevant_excerpt"), int(verified), now()),
        )

    # content history
    def add_history(self, client_id: str, run_id: str, fmt: str, topic: str, angle: str, path: str) -> None:
        self._exec(
            "INSERT INTO content_history (client_id, run_id, format, topic, angle, status, path, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (client_id, run_id, fmt, topic, angle, "pending_approval", path, now()),
        )

    def set_history_status(self, run_id: str, status: str) -> None:
        self._exec("UPDATE content_history SET status=? WHERE run_id=?", (status, run_id))

    def history(self, client_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT topic, angle, format, status, created_at FROM content_history WHERE client_id=? ORDER BY created_at",
            (client_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # spend
    def record_spend(self, run_id: str, task_id: str, agent_id: str, cost: float) -> None:
        self._exec(
            "INSERT INTO spend (run_id, task_id, agent_id, cost_usd, created_at) VALUES (?,?,?,?,?)",
            (run_id, task_id, agent_id, cost, now()),
        )

    def spent_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM spend WHERE created_at >= ?", (today,)).fetchone()
        return float(row[0])

    def spent_run(self, run_id: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM spend WHERE run_id=?", (run_id,)).fetchone()
        return float(row[0])
