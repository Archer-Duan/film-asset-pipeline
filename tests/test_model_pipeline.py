from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from film_asset_pipeline.hunyuan_config import HunyuanSettings
from film_asset_pipeline.model_pipeline import Mock3DClient, ModelBatchPipeline
from film_asset_pipeline.model_state import ModelStateStore


class ModelBatchPipelineTests(unittest.TestCase):
    def test_mock_run_creates_glb_and_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "asset.jpg"
            source.write_bytes(b"fake-jpeg")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps([{"status": "complete", "output_path": str(source)}]),
                encoding="utf-8",
            )
            settings = HunyuanSettings(
                root_dir=root,
                input_path=manifest,
                output_dir=root / "models",
                state_db=root / "models.sqlite3",
                poll_interval_seconds=1,
                job_timeout_seconds=5,
            )
            store = ModelStateStore(settings.state_db)
            try:
                pipeline = ModelBatchPipeline(settings, store, Mock3DClient())
                self.assertEqual(pipeline.scan(), (1, 1))
                self.assertEqual(pipeline.run(), {"complete": 1})
                models = list(settings.output_dir.glob("*.glb"))
                self.assertEqual(len(models), 1)
                self.assertTrue(models[0].read_bytes().startswith(b"glTF"))
                self.assertEqual(pipeline.scan(), (1, 0))
                self.assertEqual(pipeline.run(), {"complete": 1})
            finally:
                store.close()

    def test_single_image_can_be_used_for_small_poc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "asset.png"
            source.write_bytes(b"fake-png")
            settings = HunyuanSettings(
                root_dir=root,
                input_path=source,
                output_dir=root / "models",
                state_db=root / "models.sqlite3",
                poll_interval_seconds=1,
                job_timeout_seconds=5,
            )
            store = ModelStateStore(settings.state_db)
            try:
                pipeline = ModelBatchPipeline(settings, store, Mock3DClient())
                self.assertEqual(pipeline.scan(), (1, 1))
            finally:
                store.close()

    def test_failed_model_retry_can_target_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ModelStateStore(Path(directory) / "models.sqlite3")
            try:
                for task_id, source in (("task-1", "one.jpg"), ("task-2", "two.jpg")):
                    store.register_task(
                        task_id=task_id,
                        source_path=source,
                        source_sha256=task_id,
                        model="hy-3d-3.1",
                        enable_pbr=True,
                        face_count=1_000_000,
                        generate_type="Normal",
                    )
                    store.set_failed(task_id, error_code="test", error_message="failed")
                self.assertEqual(store.retry_failed("task-1"), 1)
                self.assertEqual(store.summary(), {"failed": 1, "retry": 1})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
