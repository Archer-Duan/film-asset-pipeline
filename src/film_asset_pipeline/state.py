from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    source_path: str
    source_sha256: str
    prompt: str
    mode: str
    max_images: int
    status: str
    attempts: int
    request_id: str | None
    error_code: str | None
    error_message: str | None


class StateStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def initialize(self) -> None:
        # tasks 保存任务状态，outputs 保存一项任务可能产生的多张结果；两者用外键关联。
        schema = """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            prompt TEXT NOT NULL,
            mode TEXT NOT NULL,
            max_images INTEGER NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            request_id TEXT,
            model TEXT,
            usage_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_path, source_sha256, prompt, mode, max_images)
        );
        CREATE TABLE IF NOT EXISTS outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            result_index INTEGER NOT NULL,
            output_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, result_index)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """
        with self._lock:
            self._connection.executescript(schema)
            self._connection.commit()

    def register_task(
        self,
        *,
        task_id: str,
        source_path: str,
        source_sha256: str,
        prompt: str,
        mode: str,
        max_images: int,
    ) -> bool:
        now = utc_now()
        with self._lock:
            # INSERT OR IGNORE 让扫描可以重复执行：已登记的任务不会被重新创建。
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO tasks (
                    task_id, source_path, source_sha256, prompt, mode, max_images,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (task_id, source_path, source_sha256, prompt, mode, max_images, now, now),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def runnable_tasks(self) -> list[TaskRecord]:
        # complete 不再运行；failed 只有经过 retry 命令显式确认后才会进入 retry。
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks WHERE status IN ('pending', 'retry') ORDER BY created_at"
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def set_running(self, task_id: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE tasks
                SET status='running', attempts=attempts+1, error_code=NULL,
                    error_message=NULL, updated_at=?
                WHERE task_id=?
                """,
                (utc_now(), task_id),
            )
            self._connection.commit()

    def set_complete(
        self,
        task_id: str,
        *,
        request_id: str | None,
        model: str | None,
        usage: dict[str, Any] | None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE tasks
                SET status='complete', request_id=?, model=?, usage_json=?, updated_at=?
                WHERE task_id=?
                """,
                (request_id, model, json.dumps(usage, ensure_ascii=False) if usage else None, utc_now(), task_id),
            )
            self._connection.commit()

    def set_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        request_id: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE tasks
                SET status='failed', error_code=?, error_message=?,
                    request_id=COALESCE(?, request_id), updated_at=?
                WHERE task_id=?
                """,
                (error_code, error_message, request_id, utc_now(), task_id),
            )
            self._connection.commit()

    def add_output(
        self,
        *,
        task_id: str,
        result_index: int,
        output_path: str,
        sha256: str,
        width: int | None,
        height: int | None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO outputs (
                    task_id, result_index, output_path, sha256, width, height, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, result_index, output_path, sha256, width, height, utc_now()),
            )
            self._connection.commit()

    def retry_failed(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id:
                cursor = self._connection.execute(
                    """
                    UPDATE tasks SET status='retry', updated_at=?
                    WHERE status='failed' AND task_id=?
                    """,
                    (utc_now(), task_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE tasks SET status='retry', updated_at=? WHERE status='failed'",
                    (utc_now(),),
                )
            self._connection.commit()
            return cursor.rowcount

    def recover_interrupted(self) -> int:
        # running 只代表本地进程曾开始处理，不代表远端一定没有扣费，所以转 failed 等人工确认。
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status='failed', error_code='interrupted',
                    error_message='上次运行中断；请求可能已产生费用，请确认后手动执行 retry', updated_at=?
                WHERE status='running'
                """,
                (utc_now(),),
            )
            self._connection.commit()
            return cursor.rowcount

    def summary(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def manifest_rows(self) -> list[dict[str, Any]]:
        query = """
        SELECT t.task_id, t.source_path, t.source_sha256, t.prompt, t.mode,
               t.max_images, t.status, t.attempts, t.request_id, t.model,
               t.error_code, t.error_message, t.created_at, t.updated_at,
               o.result_index, o.output_path, o.sha256 AS output_sha256,
               o.width, o.height
        FROM tasks t
        LEFT JOIN outputs o ON o.task_id = t.task_id
        ORDER BY t.created_at, o.result_index
        """
        with self._lock:
            rows: Iterable[sqlite3.Row] = self._connection.execute(query).fetchall()
        return [dict(row) for row in rows]


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        source_path=str(row["source_path"]),
        source_sha256=str(row["source_sha256"]),
        prompt=str(row["prompt"]),
        mode=str(row["mode"]),
        max_images=int(row["max_images"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        request_id=str(row["request_id"]) if row["request_id"] else None,
        error_code=str(row["error_code"]) if row["error_code"] else None,
        error_message=str(row["error_message"]) if row["error_message"] else None,
    )
