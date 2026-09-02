from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from film_asset_pipeline.web import create_app
from film_asset_pipeline.workflow import (
    WorkflowStore,
    build_asset_inventory,
    inventory_summary,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._credential_environment = {
            name: os.environ.get(name)
            for name in (
                "ARK_API_KEY",
                "TENCENTCLOUD_SECRET_ID",
                "TENCENTCLOUD_SECRET_KEY",
            )
        }
        os.environ["ARK_API_KEY"] = "test-ark-key"
        os.environ["TENCENTCLOUD_SECRET_ID"] = "test-secret-id"
        os.environ["TENCENTCLOUD_SECRET_KEY"] = "test-secret-key"

    def tearDown(self) -> None:
        for name, value in self._credential_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_inventory_links_2d_asset_to_completed_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "瓷瓶__asset.jpg"
            model = root / "瓷瓶.glb"
            preview = root / "瓷瓶-preview.jpg"
            image.write_bytes(b"jpeg")
            model.write_bytes(b"glTF")
            preview.write_bytes(b"jpeg")
            image_manifest = root / "manifest.json"
            model_manifest = root / "manifest-3d.json"
            image_manifest.write_text(
                json.dumps(
                    [
                        {
                            "task_id": "image-task",
                            "status": "complete",
                            "source_path": str(root / "frame.png"),
                            "output_path": str(image),
                            "output_sha256": "a" * 64,
                            "model": "seedream",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            model_manifest.write_text(
                json.dumps(
                    [
                        {
                            "task_id": "model-task",
                            "source_path": str(image),
                            "status": "complete",
                            "output_path": str(model),
                            "preview_path": str(preview),
                            "face_count": 500000,
                            "enable_pbr": 0,
                            "credits_consumed": 20,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            store = WorkflowStore(root / "workflow.sqlite3")
            try:
                assets = build_asset_inventory(image_manifest, model_manifest, store)
                self.assertEqual(len(assets), 1)
                self.assertEqual(assets[0]["stage"], "model_review")
                self.assertEqual(assets[0]["title"], "瓷瓶")
                self.assertEqual(inventory_summary(assets)["model_review"], 1)
            finally:
                store.close()

    def test_web_workspace_returns_public_file_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/input").mkdir(parents=True)
            (root / "data/output").mkdir(parents=True)
            (root / "data/models").mkdir(parents=True)
            image = root / "data/output/asset.jpg"
            image.write_bytes(b"jpeg")
            (root / "data/output/manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "image-task",
                            "status": "complete",
                            "source_path": str(image),
                            "output_path": str(image),
                            "output_sha256": "b" * 64,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            with TestClient(create_app(config)) as client:
                response = client.get("/api/workspace")
                self.assertEqual(response.status_code, 200)
                asset = response.json()["assets"][0]
                self.assertTrue(asset["image_url"].startswith("/api/assets/"))
                self.assertNotIn("image_path", asset)
                self.assertIn("no-store", response.headers["cache-control"])

    def test_web_workspace_includes_uploaded_frames_waiting_for_2d(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "data/input"
            (root / "data/output").mkdir(parents=True)
            (root / "data/models").mkdir(parents=True)
            input_dir.mkdir(parents=True)
            (input_dir / "古筝.png").write_bytes(b"png")
            (input_dir / "古筝__重复.png").write_bytes(b"png")
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            with TestClient(create_app(config)) as client:
                workspace = client.get("/api/workspace").json()
                self.assertEqual(workspace["summary"]["awaiting_2d"], 1)
                self.assertEqual(workspace["summary"]["source_frames"], 1)
                self.assertEqual(workspace["assets"][0]["stage"], "awaiting_2d")
                self.assertEqual(workspace["assets"][0]["title"], "古筝")
                self.assertEqual(workspace["assets"][0]["target_name"], "古筝")
                image_response = client.get(workspace["assets"][0]["image_url"])
                self.assertEqual(image_response.status_code, 200)

    def test_failed_2d_attempt_is_exposed_on_pending_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "data/input"
            input_dir.mkdir(parents=True)
            frame = input_dir / "玉佩.png"
            frame.write_bytes(b"jade")
            source_hash = __import__("hashlib").sha256(b"jade").hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    [
                        {
                            "task_id": "jade-task",
                            "status": "failed",
                            "source_path": str(frame),
                            "source_sha256": source_hash,
                            "error_code": "SetLimitExceeded",
                            "error_message": "safe mode limit",
                            "attempts": 1,
                            "updated_at": "2026-08-18T10:43:00+00:00",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            model_manifest = root / "manifest-3d.json"
            store = WorkflowStore(root / "workflow.sqlite3")
            try:
                assets = build_asset_inventory(manifest, model_manifest, store, input_dir)
                self.assertEqual(len(assets), 1)
                self.assertEqual(assets[0]["image_task_id"], "jade-task")
                self.assertEqual(assets[0]["image_error_code"], "SetLimitExceeded")
                self.assertEqual(assets[0]["image_attempts"], 1)
            finally:
                store.close()

    def test_upload_skips_duplicate_image_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/input").mkdir(parents=True)
            (root / "data/output").mkdir(parents=True)
            (root / "data/models").mkdir(parents=True)
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            with TestClient(create_app(config)) as client:
                first = client.post(
                    "/api/frames/upload", files=[("files", ("静帧.png", b"same", "image/png"))]
                ).json()
                second = client.post(
                    "/api/frames/upload", files=[("files", ("重复.png", b"same", "image/png"))]
                ).json()
                third = client.post(
                    "/api/frames/upload", files=[("files", ("静帧.png", b"different", "image/png"))]
                ).json()
                self.assertEqual(first["count"], 1)
                self.assertEqual(second["count"], 0)
                self.assertEqual(second["skipped_count"], 1)
                self.assertEqual(third["count"], 1)
                self.assertEqual(len(list((root / "data/input").glob("静帧__*.png"))), 2)

                with patch.object(Path, "write_bytes", side_effect=PermissionError("locked")):
                    failed_response = client.post(
                        "/api/frames/upload",
                        files=[("files", ("新图.png", b"new-content", "image/png"))],
                    )
                self.assertEqual(failed_response.status_code, 200)
                self.assertEqual(failed_response.json()["failed_count"], 1)

    def test_process_2d_accepts_only_selected_frame_and_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "data/input"
            (root / "data/output").mkdir(parents=True)
            (root / "data/models").mkdir(parents=True)
            input_dir.mkdir(parents=True)
            frame = input_dir / "古琴.png"
            frame.write_bytes(b"frame")
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            with patch("film_asset_pipeline.web._run_cli", return_value="ok") as run_cli:
                with TestClient(create_app(config)) as client:
                    asset_id = client.get("/api/workspace").json()["assets"][0]["asset_id"]
                    response = client.post(
                        "/api/actions/process-2d",
                        json={
                            "asset_ids": [asset_id],
                            "output_count": 3,
                            "confirmation": "PROCESS_2D",
                        },
                    )
                    self.assertEqual(response.status_code, 202)
                    job_id = response.json()["job_id"]
                    for _ in range(100):
                        job = client.get(f"/api/jobs/{job_id}").json()
                        if job["status"] in {"complete", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(job["status"], "complete")
                    self.assertEqual(job["progress"]["percent"], 100)
                    arguments = run_cli.call_args.args[1]
                    self.assertIn(str(frame.resolve()), arguments)
                    self.assertEqual(arguments[arguments.index("--mode") + 1], "group")
                    self.assertEqual(arguments[arguments.index("--max-images") + 1], "3")
                    prompt = arguments[arguments.index("--prompt") + 1]
                    self.assertIn("本次唯一目标资产是“古琴”", prompt)
                    self.assertIn("三张输出必须是同一件目标资产", prompt)
                    self.assertIn("workflow_batch:", prompt)

                    run_cli.reset_mock()
                    single_response = client.post(
                        "/api/actions/process-2d",
                        json={
                            "asset_ids": [asset_id],
                            "output_count": 1,
                            "confirmation": "PROCESS_2D",
                        },
                    )
                    single_job_id = single_response.json()["job_id"]
                    for _ in range(100):
                        single_job = client.get(f"/api/jobs/{single_job_id}").json()
                        if single_job["status"] in {"complete", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(single_job["status"], "complete")
                    single_arguments = run_cli.call_args.args[1]
                    self.assertEqual(single_arguments[single_arguments.index("--mode") + 1], "single")
                    self.assertEqual(single_arguments[single_arguments.index("--max-images") + 1], "1")
                    single_prompt = single_arguments[single_arguments.index("--prompt") + 1]
                    self.assertIn("只生成一张目标资产的完整主视图", single_prompt)

    def test_reprocess_2d_deduplicates_variants_from_same_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "data/input"
            output_dir = root / "data/output"
            model_dir = root / "data/models"
            input_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)
            model_dir.mkdir(parents=True)
            source = input_dir / "玉佩__source.png"
            source.write_bytes(b"jade-source")
            rows = []
            for index, digest in ((1, "a" * 64), (2, "b" * 64)):
                output = output_dir / f"jade-{index}.jpg"
                output.write_bytes(f"jade-{index}".encode())
                rows.append(
                    {
                        "task_id": "old-jade-task",
                        "status": "complete",
                        "source_path": str(source),
                        "source_sha256": "c" * 64,
                        "output_path": str(output),
                        "output_sha256": digest,
                        "result_index": index,
                        "updated_at": "2026-08-18T11:00:00+00:00",
                    }
                )
            (output_dir / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[ark]
prompt = "固定提示词"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            with patch("film_asset_pipeline.web._run_cli", return_value="ok") as run_cli:
                with TestClient(create_app(config)) as client:
                    assets = client.get("/api/workspace").json()["assets"]
                    self.assertEqual(len(assets), 3)
                    source_assets = [asset for asset in assets if asset["stage"] == "source_frame"]
                    self.assertEqual(len(source_assets), 1)
                    self.assertEqual(source_assets[0]["target_name"], "玉佩")
                    response = client.post(
                        "/api/actions/process-2d",
                        json={
                            "asset_ids": [asset["asset_id"] for asset in assets],
                            "confirmation": "PROCESS_2D",
                        },
                    )
                    job_id = response.json()["job_id"]
                    for _ in range(100):
                        job = client.get(f"/api/jobs/{job_id}").json()
                        if job["status"] in {"complete", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(job["status"], "complete")
                    self.assertEqual(run_cli.call_count, 1)
                    arguments = run_cli.call_args.args[1]
                    prompt = arguments[arguments.index("--prompt") + 1]
                    self.assertIn("本次唯一目标资产是“玉佩”", prompt)

    def test_batch_review_moves_assets_through_expected_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "data/input"
            output_dir = root / "data/output"
            model_dir = root / "data/models"
            input_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)
            model_dir.mkdir(parents=True)
            source = input_dir / "玉佩.png"
            source.write_bytes(b"source")
            rows = []
            for index in (1, 2):
                output = output_dir / f"jade-{index}.jpg"
                output.write_bytes(f"jade-{index}".encode())
                rows.append(
                    {
                        "task_id": "jade-task",
                        "status": "complete",
                        "source_path": str(source),
                        "source_sha256": "c" * 64,
                        "output_path": str(output),
                        "output_sha256": str(index) * 64,
                        "result_index": index,
                        "updated_at": f"2026-08-18T10:0{index}:00+00:00",
                    }
                )
            (output_dir / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            with TestClient(create_app(config)) as client:
                assets = [
                    asset
                    for asset in client.get("/api/workspace").json()["assets"]
                    if asset["stage"] == "image_review"
                ]
                asset_ids = [asset["asset_id"] for asset in assets]
                self.assertEqual(len(asset_ids), 2)
                self.assertEqual(assets[0]["generated_at"], "2026-08-18T10:01:00+00:00")

                approved = client.post(
                    "/api/actions/review",
                    json={"asset_ids": asset_ids, "stage": "image", "decision": "approved"},
                )
                self.assertEqual(approved.status_code, 200)
                self.assertEqual(approved.json()["updated_count"], 2)
                ready = {
                    asset["asset_id"]: asset["stage"]
                    for asset in client.get("/api/workspace").json()["assets"]
                }
                self.assertTrue(all(ready[asset_id] == "ready_for_3d" for asset_id in asset_ids))

                invalid = client.post(
                    "/api/actions/review",
                    json={"asset_ids": asset_ids, "stage": "image", "decision": "rejected"},
                )
                self.assertEqual(invalid.status_code, 409)

                client.post(
                    f"/api/assets/{asset_ids[0]}/review",
                    json={"stage": "image", "decision": "rejected"},
                )
                resubmitted = client.post(
                    "/api/actions/review",
                    json={"asset_ids": [asset_ids[0]], "stage": "auto", "decision": "pending"},
                )
                self.assertEqual(resubmitted.status_code, 200)
                restored = next(
                    asset
                    for asset in client.get("/api/workspace").json()["assets"]
                    if asset["asset_id"] == asset_ids[0]
                )
                self.assertEqual(restored["stage"], "image_review")

    def test_3d_batch_reports_each_selected_asset_and_continues_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "data/input"
            output_dir = root / "data/output"
            model_dir = root / "data/models"
            input_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)
            model_dir.mkdir(parents=True)
            source = input_dir / "玉佩.png"
            source.write_bytes(b"source")
            image_rows = []
            for index in (1, 2):
                output = output_dir / f"jade-{index}.jpg"
                output.write_bytes(f"jade-{index}".encode())
                image_rows.append(
                    {
                        "task_id": "jade-task",
                        "status": "complete",
                        "source_path": str(source),
                        "source_sha256": "c" * 64,
                        "output_path": str(output),
                        "output_sha256": str(index) * 64,
                        "result_index": index,
                        "updated_at": f"2026-08-18T10:0{index}:00+00:00",
                    }
                )
            (output_dir / "manifest.json").write_text(
                json.dumps(image_rows), encoding="utf-8"
            )
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            model_rows: list[dict[str, object]] = []

            def fake_run_cli(_: Path, arguments: list[str]) -> str:
                self.assertEqual(arguments[0], "run-3d")
                image_path = Path(arguments[arguments.index("--input") + 1]).resolve()
                failed = image_path.name == "jade-2.jpg"
                model_rows.append(
                    {
                        "task_id": f"model-{image_path.stem}",
                        "status": "failed" if failed else "complete",
                        "source_path": str(image_path),
                        "enable_pbr": False,
                        "face_count": 500_000,
                        "error_code": "ResourceInsufficient" if failed else None,
                        "error_message": "资源不足" if failed else None,
                        "request_id": "request-jade-2" if failed else None,
                        "updated_at": f"2026-08-18T11:0{len(model_rows)}:00+00:00",
                    }
                )
                (model_dir / "manifest-3d.json").write_text(
                    json.dumps(model_rows), encoding="utf-8"
                )
                # 模拟 CLI 因全库中存在失败记录而返回非零退出码。
                raise RuntimeError('{"complete": 5, "failed": 2}')

            with patch("film_asset_pipeline.web._run_cli", side_effect=fake_run_cli) as run_cli:
                with TestClient(create_app(config)) as client:
                    assets = [
                        asset
                        for asset in client.get("/api/workspace").json()["assets"]
                        if asset["stage"] == "image_review"
                    ]
                    asset_ids = [asset["asset_id"] for asset in assets]
                    approved = client.post(
                        "/api/actions/review",
                        json={"asset_ids": asset_ids, "stage": "image", "decision": "approved"},
                    )
                    self.assertEqual(approved.status_code, 200)
                    response = client.post(
                        "/api/actions/generate-3d",
                        json={
                            "asset_ids": asset_ids,
                            "profile": "minimal",
                            "confirmation": "GENERATE_3D",
                        },
                    )
                    job_id = response.json()["job_id"]
                    for _ in range(100):
                        job = client.get(f"/api/jobs/{job_id}").json()
                        if job["status"] in {"complete", "failed"}:
                            break
                        time.sleep(0.01)

            self.assertEqual(run_cli.call_count, 2)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["progress"]["completed"], 2)
            self.assertEqual(job["progress"]["succeeded"], 1)
            self.assertEqual(job["progress"]["failed"], 1)
            self.assertIn("ResourceInsufficient", job["message"])
            self.assertIn("资源不足", job["message"])
            self.assertIn("request-jade-2", job["message"])
            self.assertNotIn('"complete": 5', job["message"])


if __name__ == "__main__":
    unittest.main()
