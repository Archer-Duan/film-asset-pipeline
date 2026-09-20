import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from film_asset_pipeline.asset_files import task_prefix
from film_asset_pipeline.local_engine import LocalEngine
from film_asset_pipeline.organize_outputs import plan_organization, apply_organization
from film_asset_pipeline.team_auth import TeamAuth
from film_asset_pipeline.web import create_app
from film_asset_pipeline.workflow import (
    WorkflowStore,
    build_asset_inventory,
    inventory_summary,
)
from test_local_comfy import FakeComfy
from test_workflow_library import bundle, SCHEMA


class AssetDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comfy = FakeComfy()
        self.engine = LocalEngine(self.root, self.comfy)
        self.source = self.root / "uploads" / "原图.png"
        self.source.parent.mkdir()
        self.source.write_bytes(self.comfy.png)
        self.store = WorkflowStore(self.root / "review.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def submit(self, kind="edit"):
        with patch.object(self.engine, "prepare", return_value={}):
            self.engine.submit([self.source], kind, title="青铜罐")
        return self.engine.rows()[0]

    def inventory(self):
        images, models = self.engine.manifest_rows()
        return build_asset_inventory(
            self.root / "none.json",
            self.root / "none-3d.json",
            self.store,
            None,
            images,
            models,
            self.engine.source_rows(),
        )

    def test_upload_queue_result_and_review_share_bidirectional_links(self):
        task = self.submit()
        assets = self.inventory()
        self.assertEqual(assets[0]["stage"], "image_generation")
        source_id = assets[0]["asset_id"]
        node = next(
            k
            for k, role in json.loads(task["profile"])["outputs"].items()
            if role == "image"
        )
        self.engine.collect(task, {node: {"images": [{"filename": "out.png"}]}})
        assets = self.inventory()
        self.assertIn(source_id, [a["asset_id"] for a in assets])
        self.assertEqual(
            next(a for a in assets if a["asset_id"] == source_id)["stage"],
            "source_frame",
        )

    def test_completed_image_keeps_original_and_multiple_results(self):
        task = self.submit()
        node = next(
            k
            for k, role in json.loads(task["profile"])["outputs"].items()
            if role == "image"
        )
        self.engine.collect(task, {node: {"images": [{"filename": "one.png"}]}})
        assets = self.inventory()
        source = next(a for a in assets if a["is_source"])
        result = next(a for a in assets if not a["is_source"])
        self.assertEqual(result["source_asset_ids"], [source["asset_id"]])
        self.assertEqual(source["related_asset_ids"], [result["asset_id"]])
        self.assertEqual(result["stage"], "image_review")
        self.assertEqual(result["title"], "青铜罐")
        self.assertEqual(inventory_summary(assets)["total"], len(assets))
        self.assertIn(task["id"], source["task_ids"])
        self.assertIn("results", Path(result["image_path"]).parts)

    def test_multiple_models_from_one_task_are_all_reviewable_and_downloadable(self):
        task = self.submit("model")
        node = next(
            k
            for k, role in json.loads(task["profile"])["outputs"].items()
            if role == "model"
        )
        self.engine.collect(
            task,
            {node: {"models": [{"filename": "first.glb"}, {"filename": "second.glb"}]}},
        )
        results = [a for a in self.inventory() if not a["is_source"]]
        self.assertEqual(len(results), 2)
        self.assertEqual(len({a["asset_id"] for a in results}), 2)
        self.assertEqual(len({a["model_path"] for a in results}), 2)
        self.assertTrue(all(a["stage"] == "model_review" for a in results))

    def test_duplicate_upload_paths_link_to_the_same_source_and_all_tasks(self):
        first = self.submit("model")
        self.engine.update(first["id"], status="failed")
        other = self.root / "duplicate.png"
        other.write_bytes(self.source.read_bytes())
        with patch.object(self.engine, "prepare", return_value={}):
            self.engine.submit([other], "model", owner="colleague")
        assets = self.inventory()
        sources = [a for a in assets if a["is_source"]]
        results = [a for a in assets if not a["is_source"]]
        self.assertEqual(len(sources), 1)
        self.assertEqual(len(sources[0]["task_ids"]), 2)
        self.assertEqual(results[0]["source_asset_ids"], [sources[0]["asset_id"]])
        self.assertEqual(len(results[0]["task_ids"]), 2)

    def test_failed_edit_appears_in_failure_stage_with_retry_context(self):
        task = self.submit()
        self.engine.update(task["id"], status="failed", message="模型未加载")
        asset = self.inventory()[0]
        self.assertEqual(asset["stage"], "generation_failed")
        self.assertEqual(asset["generation_error"], "模型未加载")
        self.assertEqual(asset["task_ids"], [task["id"]])

    def test_output_prefixes_cover_intermediate_savers_and_midnight(self):
        task = {
            "created": "2026-09-19T16:01:02+00:00",
            "source": "reference.png",
            "title": "../青铜:罐",
            "kind": "edit",
            "id": "abcdef012345",
        }
        prefix = {r: task_prefix(task, r) for r in ("image", "model")}
        self.assertTrue(prefix["image"].startswith("2026-09-20_2d/"))
        self.assertNotIn("..", prefix["image"])
        b = bundle()
        b["workflow"]["2"]["inputs"]["image"] = ["5", 0]
        b["workflow"]["5"] = {
            "class_type": "SaveImage",
            "inputs": {"images": ["1", 0], "filename_prefix": "front_view"},
        }
        self.engine.library.import_bundle(b)
        profile, workflow = self.engine.profile("custom-image")
        with patch.object(self.comfy, "request", return_value=SCHEMA):
            graph = self.engine.prepare(
                "custom-image", prefix=prefix, profile=profile, workflow=workflow
            )
        for node in ("3", "5"):
            self.assertTrue(
                graph[node]["inputs"]["filename_prefix"].startswith(prefix["image"])
            )
        self.assertIn("正面-5", graph["5"]["inputs"]["filename_prefix"])

    def test_organize_preserves_bytes_review_and_unrelated_files_and_is_repeatable(
        self,
    ):
        task = self.submit("model")
        legacy = self.engine.folder / task["id"] / "00001.glb"
        legacy.parent.mkdir()
        legacy.write_bytes(b"model-payload")
        self.engine.update(task["id"], model=str(legacy), status="complete")
        asset = next(a for a in self.inventory() if not a["is_source"])
        self.store.update(
            asset["asset_id"], model_review="approved", title="已审核青铜罐"
        )
        output = self.root / "comfy-output"
        native = output / "workbench" / task["id"] / "00001.glb"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"model-payload")
        unrelated = output / "other.png"
        unrelated.write_bytes(b"unrelated")
        plan = plan_organization(self.engine, output)
        result = apply_organization(self.engine, output, plan)
        self.assertEqual(result, {"cached": 1, "native": 1})
        self.assertFalse(native.exists())
        self.assertTrue(unrelated.exists())
        moved = next(output.glob("*_3d/*.glb"))
        self.assertEqual(moved.read_bytes(), b"model-payload")
        self.assertIn("青铜罐", moved.name)
        reviewed = next(a for a in self.inventory() if not a["is_source"])
        self.assertEqual(reviewed["asset_id"], asset["asset_id"])
        self.assertEqual(reviewed["stage"], "approved")
        self.assertEqual(reviewed["title"], "已审核青铜罐")
        apply_organization(self.engine, output, plan_organization(self.engine, output))
        self.assertEqual(len(list(output.glob("*_3d/*.glb"))), 1)


class DownloadApiTests(unittest.TestCase):
    def test_zip_names_manifest_missing_selection_and_member_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                '[paths]\ninput_dir="data/input"\noutput_dir="data/output"\nmodel_output_dir="data/models"\n',
                encoding="utf-8",
            )
            folder = root / "data/output"
            folder.mkdir(parents=True)
            rows, models = [], []
            for i in range(2):
                image = folder / f"{i}.png"
                image.write_bytes(f"image-{i}".encode())
                model = folder / f"{i}.glb"
                model.write_bytes(f"model-{i}".encode())
                rows.append(
                    {
                        "task_id": str(i),
                        "status": "complete",
                        "output_path": str(image),
                        "output_sha256": str(i) * 64,
                        "title": "青铜罐",
                    }
                )
                models.append(
                    {
                        "task_id": str(i),
                        "status": "complete",
                        "source_path": str(image),
                        "output_path": str(model),
                    }
                )
            (folder / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
            model_dir = root / "data/models"
            model_dir.mkdir()
            (model_dir / "manifest-3d.json").write_text(
                json.dumps(models), encoding="utf-8"
            )
            auth = TeamAuth(root)
            auth.add_user("artist", "test-password-1234")
            with TestClient(create_app(config)) as client:
                assets = client.get("/api/workspace").json()["assets"]
                ids = [a["asset_id"] for a in assets]
                response = client.post(
                    "/api/actions/download", json={"asset_ids": ids, "kind": "model"}
                )
                self.assertEqual(
                    response.status_code,
                    200,
                    response.text[:100] if response.status_code != 200 else "",
                )
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    names = [n for n in archive.namelist() if n.endswith(".glb")]
                    self.assertEqual(len(set(names)), 2)
                    self.assertTrue(all(n.startswith("青铜罐__模型") for n in names))
                    self.assertEqual(
                        {archive.read(n) for n in names}, {b"model-0", b"model-1"}
                    )
                    self.assertEqual(len(json.loads(archive.read("资产清单.json"))), 2)
                self.assertFalse(list((root / "data/local-assets/downloads").iterdir()))
                self.assertEqual(
                    client.post(
                        "/api/actions/download",
                        json={"asset_ids": ids + ["missing"], "kind": "model"},
                    ).status_code,
                    404,
                )
                Path(models[0]["output_path"]).unlink()
                self.assertEqual(
                    client.post(
                        "/api/actions/download",
                        json={"asset_ids": ids, "kind": "model"},
                    ).status_code,
                    409,
                )
                client.post(
                    "/api/team/login",
                    json={"username": "artist", "password": "test-password-1234"},
                )
                self.assertEqual(
                    client.get("/api/team/me").json(),
                    {"username": "artist", "admin": False},
                )
                self.assertEqual(
                    client.post(
                        "/api/team/members",
                        json={
                            "username": "unauthorized",
                            "password": "test-password-1234",
                        },
                    ).status_code,
                    403,
                )
