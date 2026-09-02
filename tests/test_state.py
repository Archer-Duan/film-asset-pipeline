from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from film_asset_pipeline.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_failed_tasks_can_be_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                store.register_task(
                    task_id="task-1",
                    source_path="frame.png",
                    source_sha256="abc",
                    prompt="prompt",
                    mode="single",
                    max_images=1,
                )
                store.set_running("task-1")
                store.set_failed("task-1", error_code="test", error_message="failed")
                self.assertEqual(store.summary(), {"failed": 1})
                self.assertEqual(store.retry_failed(), 1)
                self.assertEqual(store.summary(), {"retry": 1})
            finally:
                store.close()

    def test_retry_failed_can_target_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                for task_id in ("task-a", "task-b"):
                    store.register_task(
                        task_id=task_id,
                        source_path=f"{task_id}.png",
                        source_sha256=task_id,
                        prompt="prompt",
                        mode="single",
                        max_images=1,
                    )
                    store.set_failed(task_id, error_code="test", error_message="failed")
                self.assertEqual(store.retry_failed("task-a"), 1)
                statuses = {row["task_id"]: row["status"] for row in store.manifest_rows()}
                self.assertEqual(statuses["task-a"], "retry")
                self.assertEqual(statuses["task-b"], "failed")
            finally:
                store.close()

    def test_interrupted_tasks_require_manual_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                store.register_task(
                    task_id="task-2",
                    source_path="frame.png",
                    source_sha256="def",
                    prompt="prompt",
                    mode="group",
                    max_images=3,
                )
                store.set_running("task-2")
                self.assertEqual(store.recover_interrupted(), 1)
                self.assertEqual(store.summary(), {"failed": 1})
                self.assertEqual(store.retry_failed(), 1)
                self.assertEqual(store.summary(), {"retry": 1})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
