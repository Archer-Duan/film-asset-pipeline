from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import utc_now


@dataclass(frozen=True)
class ModelTask:
    task_id: str
    source_path: str
    source_sha256: str
    model: str
    enable_pbr: bool
    face_count: int
    generate_type: str
    status: str
    attempts: int
    remote_job_id: str | None
    request_id: str | None


class ModelStateStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
        self.initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_tasks (
                    task_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL,
                    enable_pbr INTEGER NOT NULL,
                    face_count INTEGER NOT NULL,
                    generate_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    remote_job_id TEXT,
                    request_id TEXT,
                    output_path TEXT,
                    preview_path TEXT,
                    credits_consumed REAL,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_path, source_sha256, model, enable_pbr, face_count, generate_type)
                );
                CREATE INDEX IF NOT EXISTS idx_model_tasks_status ON model_tasks(status);
                """
            )
            self._connection.commit()

    def register_task(
        self,
        *,
        task_id: str,
        source_path: str,
        source_sha256: str,
        model: str,
        enable_pbr: bool,
        face_count: int,
        generate_type: str,
    ) -> bool:
        now = utc_now()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO model_tasks (
                    task_id, source_path, source_sha256, model, enable_pbr,
                    face_count, generate_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    task_id,
                    source_path,
                    source_sha256,
                    model,
                    int(enable_pbr),
                    face_count,
                    generate_type,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def runnable_tasks(self) -> list[ModelTask]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM model_tasks
                WHERE status IN ('pending', 'retry', 'submitted', 'running')
                ORDER BY created_at
                """
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def set_submitting(self, task_id: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE model_tasks SET status='submitting', attempts=attempts+1,
                    error_code=NULL, error_message=NULL, updated_at=? WHERE task_id=?
                """,
                (utc_now(), task_id),
            )
            self._connection.commit()

    def set_submitted(self, task_id: str, *, job_id: str, request_id: str | None) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE model_tasks SET status='submitted', remote_job_id=?, request_id=?,
                    updated_at=? WHERE task_id=?
                """,
                (job_id, request_id, utc_now(), task_id),
            )
            self._connection.commit()

    def set_running(self, task_id: str, *, request_id: str | None = None) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE model_tasks SET status='running', request_id=COALESCE(?, request_id),
                    updated_at=? WHERE task_id=?
                """,
                (request_id, utc_now(), task_id),
            )
            self._connection.commit()

    def set_waiting(self, task_id: str, message: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE model_tasks SET status='submitted', error_code='poll_timeout',
                    error_message=?, updated_at=? WHERE task_id=?
                """,
                (message, utc_now(), task_id),
            )
            self._connection.commit()

    def set_complete(
        self,
        task_id: str,
        *,
        output_path: str,
        preview_path: str | None,
        request_id: str | None,
        credits_consumed: float | None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE model_tasks SET status='complete', output_path=?, preview_path=?,
                    request_id=COALESCE(?, request_id), credits_consumed=?,
                    error_code=NULL, error_message=NULL, updated_at=? WHERE task_id=?
                """,
                (
                    output_path,
                    preview_path,
                    request_id,
                    credits_consumed,
                    utc_now(),
                    task_id,
                ),
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
                UPDATE model_tasks SET status='failed', error_code=?, error_message=?,
                    request_id=COALESCE(?, request_id), updated_at=? WHERE task_id=?
                """,
                (error_code, error_message, request_id, utc_now(), task_id),
            )
            self._connection.commit()

    def recover_submitting(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE model_tasks SET status='failed', error_code='submit_interrupted',
                    error_message='提交过程中断，无法确认是否已计费；请人工核对后再 retry',
                    updated_at=? WHERE status='submitting' AND remote_job_id IS NULL
                """,
                (utc_now(),),
            )
            self._connection.commit()
            return cursor.rowcount

    def retry_failed(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id:
                cursor = self._connection.execute(
                    """
                    UPDATE model_tasks SET status='retry', remote_job_id=NULL,
                        error_code=NULL, error_message=NULL, updated_at=?
                    WHERE status='failed' AND task_id=?
                    """,
                    (utc_now(), task_id),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE model_tasks SET status='retry', remote_job_id=NULL,
                        error_code=NULL, error_message=NULL, updated_at=? WHERE status='failed'
                    """,
                    (utc_now(),),
                )
            self._connection.commit()
            return cursor.rowcount

    def summary(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM model_tasks GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def manifest_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM model_tasks ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]


def _task_from_row(row: sqlite3.Row) -> ModelTask:
    return ModelTask(
        task_id=str(row["task_id"]),
        source_path=str(row["source_path"]),
        source_sha256=str(row["source_sha256"]),
        model=str(row["model"]),
        enable_pbr=bool(row["enable_pbr"]),
        face_count=int(row["face_count"]),
        generate_type=str(row["generate_type"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        remote_job_id=str(row["remote_job_id"]) if row["remote_job_id"] else None,
        request_id=str(row["request_id"]) if row["request_id"] else None,
    )
