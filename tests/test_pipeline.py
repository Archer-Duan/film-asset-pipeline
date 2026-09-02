from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from film_asset_pipeline.config import Settings
from film_asset_pipeline.pipeline import BatchPipeline, MockImageClient
from film_asset_pipeline.state import StateStore


class BatchPipelineTests(unittest.TestCase):
    def test_scan_accepts_one_selected_input_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "selected.png"
            image.write_bytes(b"png")
            settings = Settings(
                root_dir=root,
                input_dir=image,
                output_dir=root / "output",
                state_db=root / "state.sqlite3",
                api_key="",
                mode="single",
                max_images=1,
            )
            store = StateStore(settings.state_db)
            try:
                pipeline = BatchPipeline(settings, store, MockImageClient())
                self.assertEqual(pipeline.scan(), (1, 1))
                self.assertEqual(pipeline.run(), {"complete": 1})
            finally:
                store.close()

    def test_mock_group_run_is_resumable_and_exports_two_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "shot-001.png").write_bytes(b"\x89PNG\r\n\x1a\nmock")
            settings = Settings(
                root_dir=root,
                input_dir=input_dir,
                output_dir=output_dir,
                state_db=root / "state.sqlite3",
                api_key="",
                mode="group",
                max_images=3,
                concurrency=2,
            )
            store = StateStore(settings.state_db)
            try:
                pipeline = BatchPipeline(settings, store, MockImageClient())
                found, added = pipeline.scan()
                self.assertEqual((found, added), (1, 1))
                summary = pipeline.run()
                self.assertEqual(summary, {"complete": 1})

                outputs = sorted(output_dir.glob("*__2d-*.png"))
                self.assertEqual(len(outputs), 2)
                manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(len(manifest), 2)
                self.assertTrue(all(row["status"] == "complete" for row in manifest))

                found_again, added_again = pipeline.scan()
                self.assertEqual((found_again, added_again), (1, 0))
                summary_again = pipeline.run()
                self.assertEqual(summary_again, {"complete": 1})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
