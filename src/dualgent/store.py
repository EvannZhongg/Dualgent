"""SQLite commits the execution intent before any external input is dispatched."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from .contracts import Surface, identity

TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"observing", "thinking", "acting", "verifying"}


def now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix(".lock").open("a+b")
        self.lock.seek(0, 2)
        if self.lock.tell() == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("此数据目录已由另一个 Dualgent 服务使用。") from None
        # All operations remain synchronous on the service loop; app construction may use
        # an embedding thread before the ASGI lifespan starts on its own loop.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, state TEXT NOT NULL,
                surfaces TEXT NOT NULL, max_steps INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
        """)

    def create(self, goal: str, surfaces: list[Surface], max_steps: int) -> dict:
        task_id, timestamp = identity(), now()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,goal,state,surfaces,max_steps,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    task_id,
                    goal,
                    "queued",
                    json.dumps([s.model_dump() for s in surfaces]),
                    max_steps,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(task_id, "created", {"goal": goal, "state": "queued"})
        return self.get(task_id)

    def _event(self, task_id: str, kind: str, data: dict):
        self.db.execute(
            "INSERT INTO events(task_id,kind,data,created_at) VALUES(?,?,?,?)",
            (task_id, kind, json.dumps(data, ensure_ascii=False), now()),
        )

    def event(self, task_id: str, kind: str, data: dict):
        with self.db:
            self._event(task_id, kind, data)

    def transition(self, task_id: str, state: str, result: str = ""):
        current = self.get(task_id)["state"]
        if current in TERMINAL:
            raise ValueError(f"terminal task cannot transition: {current}")
        allowed = {
            "queued": {"observing", "cancelled", "needs_attention", "failed"},
            "observing": {"thinking", "needs_attention", "failed", "cancelled"},
            "thinking": {
                "acting",
                "observing",
                "verifying",
                "needs_attention",
                "failed",
                "cancelled",
            },
            "acting": {"verifying", "needs_attention", "failed", "cancelled"},
            "verifying": {"observing", "succeeded", "needs_attention", "failed", "cancelled"},
            "needs_attention": {"queued", "cancelled"},
        }
        if state not in allowed[current]:
            raise ValueError(f"invalid task transition: {current} -> {state}")
        with self.db:
            self.db.execute(
                "UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?",
                (state, result, now(), task_id),
            )
            self._event(task_id, "state", {"state": state, "detail": result})

    def get(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = dict(row)
        task["surfaces"] = json.loads(task["surfaces"])
        task["cancel_requested"] = bool(task["cancel_requested"])
        return task

    def list(self) -> list[dict]:
        return [
            self.get(r[0]) for r in self.db.execute("SELECT id FROM tasks ORDER BY created_at DESC")
        ]

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        return [
            {**dict(row), "data": json.loads(row["data"])}
            for row in self.db.execute(
                "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq", (task_id, after)
            )
        ]

    def cancel(self, task_id: str):
        task = self.get(task_id)
        if task["state"] in TERMINAL:
            return
        with self.db:
            self.db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task_id,))
            self._event(task_id, "cancel_requested", {})
        if task["state"] in {"queued", "needs_attention"}:
            self.transition(task_id, "cancelled", "用户已取消")

    def resume(self, task_id: str, note: str):
        if self.get(task_id)["state"] != "needs_attention":
            raise ValueError("only a task needing attention can resume")
        with self.db:
            self.db.execute("UPDATE tasks SET cancel_requested=0 WHERE id=?", (task_id,))
            self._event(task_id, "user_note", {"text": note})
        self.transition(task_id, "queued")

    def recover(self):
        for task in self.list():
            if task["state"] in ACTIVE:
                self.transition(
                    task["id"],
                    "needs_attention",
                    "服务中断；操作结果可能未知。请检查环境后继续，不会自动重放。",
                )

    def close(self):
        self.db.close()
        self.lock.close()
