import copy
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from film_asset_pipeline.comfy import compile_workflow
from film_asset_pipeline.local_engine import LocalEngine
from film_asset_pipeline.team_auth import TeamAuth
from film_asset_pipeline.web import create_app


class FakeComfy:
    def __init__(self):
        self.submissions = []
        self.history = {}
        self.queue = []
        out = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(out, "PNG")
        self.png = out.getvalue()

    def upload(self, path):
        return "input.png"

    def request(self, route, payload=None):
        if route == "/queue":
            return {"queue_running": self.queue, "queue_pending": []}
        if route == "/prompt":
            self.submissions.append(payload)
            self.queue = [[0, "remote-1", payload["prompt"], payload["extra_data"]]]
            return {"prompt_id": "remote-1"}
        if route.startswith("/history"):
            return self.history
        raise AssertionError(route)

    def download(self, file):
        if file["filename"].endswith(".png"):
            return self.png
        chunk = b'{"asset":{"version":"2.0"}}  '
        return (
            struct.pack("<4sII", b"glTF", 2, 20 + len(chunk))
            + struct.pack("<I4s", len(chunk), b"JSON")
            + chunk
        )


class CompilerTests(unittest.TestCase):
    def test_subgraph_input_override_reaches_text_encoder_without_mutating_json(self):
        def node(i, typ, ins, vals=None):
            return {
                "id": i,
                "type": typ,
                "inputs": ins,
                "widgets_values_named": vals or {},
            }

        graph = {
            "nodes": [
                node(
                    1,
                    "sub",
                    [{"name": "prompt", "widget": {"name": "prompt"}}],
                    {"prompt": "old"},
                ),
                node(2, "Save", [{"name": "image", "link": 1}]),
            ],
            "links": [[1, 1, 0, 2, 0, "IMAGE"]],
            "definitions": {
                "subgraphs": [
                    {
                        "id": "sub",
                        "inputs": [{"name": "prompt"}],
                        "outputs": [{"linkIds": [2]}],
                        "nodes": [node(3, "Encode", [{"name": "text", "link": 1}])],
                        "links": [
                            {"id": 1, "origin_id": -10, "origin_slot": 0},
                            {"id": 2, "origin_id": 3, "origin_slot": 0},
                        ],
                    }
                ]
            },
        }
        original = copy.deepcopy(graph)
        schema = {
            "Save": {"input": {"required": {"image": ["IMAGE"]}}},
            "Encode": {"input": {"required": {"text": ["STRING"]}}},
        }
        result = compile_workflow(graph, schema, [2], {(1, "prompt"): "new"})
        self.assertEqual(result["1_3"]["inputs"]["text"], "new")
        self.assertEqual(graph, original)

    def test_disabled_dependency_is_rejected(self):
        graph = {
            "nodes": [{"id": 1, "type": "Save", "mode": 4, "inputs": []}],
            "links": [],
        }
        with self.assertRaises(ValueError):
            compile_workflow(graph, {}, [1], {})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = FakeComfy()
        self.source = self.root / "input.png"
        self.source.write_bytes(self.client.png)
        self.engine = LocalEngine(self.root, self.client)
        self.prepare = patch.object(
            self.engine,
            "prepare",
            return_value={"272": {"class_type": "SaveGLB", "inputs": {}}},
        )
        self.prepare.start()

    def tearDown(self):
        self.prepare.stop()
        self.tmp.cleanup()

    def test_duplicate_batches_share_task_without_empty_batch(self):
        first = self.engine.submit([self.source, self.source], "model")
        second = self.engine.submit([self.source, self.source], "model")
        self.assertEqual(len(self.engine.rows()), 1)
        self.assertEqual(first["progress"]["total"], 1)
        self.assertEqual(second["progress"]["total"], 1)

    def test_resume_existing_remote_task_and_archive(self):
        self.engine.submit([self.source], "model")
        self.engine.tick()
        self.assertEqual(len(self.client.submissions), 1)
        restored = LocalEngine(self.root, self.client)
        restored.tick()
        self.assertEqual(len(self.client.submissions), 1)
        self.client.history = {
            "remote-1": {
                "status": {"completed": True},
                "outputs": {
                    "272": {
                        "3d": [
                            {
                                "filename": "model.glb",
                                "subfolder": "workbench",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        }
        restored.tick()
        row = restored.rows()[0]
        self.assertEqual(row["status"], "complete")
        self.assertTrue(Path(row["model"]).is_file())

    def test_ambiguous_submit_never_repeats_post(self):
        self.engine.submit([self.source], "model")
        row = self.engine.rows()[0]
        self.engine.update(row["id"], status="submitting")
        self.engine.tick()
        self.assertEqual(self.engine.rows()[0]["status"], "attention")
        self.assertEqual(self.client.submissions, [])

    def test_reconcile_post_response_lost(self):
        self.engine.submit([self.source], "model")
        row = self.engine.rows()[0]
        self.engine.update(row["id"], status="submitting")
        self.client.queue = [
            [0, "already-submitted", {}, {"workbench_task": row["id"]}]
        ]
        self.engine.tick()
        self.assertEqual(self.engine.rows()[0]["remote_id"], "already-submitted")
        self.assertEqual(self.client.submissions, [])

    def test_failed_output_can_retry_without_resubmitting_generation(self):
        self.engine.submit([self.source], "model")
        self.engine.tick()
        self.client.history = {
            "remote-1": {"status": {"completed": True}, "outputs": {}}
        }
        self.engine.tick()
        self.assertEqual(self.engine.rows()[0]["status"], "waiting")
        self.engine.tick()
        self.assertEqual(len(self.client.submissions), 1)

    def test_missing_original_file_does_not_break_inventory(self):
        self.engine.submit([self.source], "model")
        self.source.unlink()
        self.assertEqual(self.engine.manifest_rows(), ([], []))
        self.engine.tick()
        self.assertEqual(self.engine.rows()[0]["status"], "failed")

    def test_edit_review_then_model_keeps_one_asset_and_original(self):
        from film_asset_pipeline.workflow import WorkflowStore, build_asset_inventory

        self.engine.submit([self.source], "edit", "preserve shape")
        edit = self.engine.rows()[0]
        image = self.root / "edited.png"
        image.write_bytes(self.client.png)
        self.engine.update(edit["id"], status="complete", image=str(image))
        metadata = WorkflowStore(self.root / "review.sqlite3")
        try:

            def inventory():
                images, models = self.engine.manifest_rows()
                return build_asset_inventory(
                    self.root / "unused.json",
                    self.root / "unused-3d.json",
                    metadata,
                    extra_images=images,
                    extra_models=models,
                )

            before = inventory()
            self.assertEqual(len(before), 1)
            self.assertEqual(before[0]["stage"], "image_review")
            asset_id = before[0]["asset_id"]
            metadata.update(asset_id, image_review="approved", title="Bronze vessel")
            self.engine.submit([image], "model", originals=[self.source])
            after = inventory()
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0]["asset_id"], asset_id)
            self.assertEqual(after[0]["source_path"], str(self.source))
            self.assertEqual(after[0]["title"], "Bronze vessel")
            self.assertEqual(after[0]["stage"], "model_generation")
        finally:
            metadata.close()

    def test_multiview_stays_disabled_without_model_download(self):
        with self.assertRaises(ValueError):
            self.engine.submit([self.source], "multiview")
        self.assertEqual(self.engine.rows(), [])


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.toml").write_text("", encoding="utf-8")
        self.app = create_app(self.root / "config.toml")
        self.app.state.local_engine.config["enabled"] = True

    def tearDown(self):
        with TestClient(self.app):
            pass
        self.tmp.cleanup()

    def test_remote_requires_login_and_cannot_administer(self):
        auth = TeamAuth(self.root)
        auth.add_user("artist", "long-enough-password")
        with TestClient(self.app, client=("192.168.1.20", 1234)) as client:
            self.assertEqual(client.get("/api/workspace").status_code, 401)
            self.assertEqual(client.get("/objects").status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/team/login", json={"username": "artist", "password": "wrong"}
                ).status_code,
                401,
            )
            self.assertEqual(
                client.post(
                    "/api/team/login",
                    json={"username": "artist", "password": "long-enough-password"},
                ).status_code,
                200,
            )
            self.assertEqual(client.get("/api/workspace").status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/team/members",
                    json={"username": "bad", "password": "long-enough-password"},
                ).status_code,
                403,
            )
            self.assertEqual(client.get("/api/settings/status").status_code, 403)
            self.assertEqual(
                client.post(
                    "/api/local/tasks/anything/retry",
                    headers={"Origin": "https://other.example"},
                ).status_code,
                403,
            )
            client.post("/api/team/logout")
            self.assertEqual(client.get("/api/local/tasks").status_code, 401)

    def test_valid_upload_records_owner_and_never_calls_cloud(self):
        with (
            TestClient(self.app) as client,
            patch.object(self.app.state.local_engine, "prepare", return_value={}),
        ):
            image = FakeComfy().png
            response = client.post(
                "/api/local/tasks",
                files=[("files", ("reference.png", image, "image/png"))],
                data={"kind": "edit", "prompt": "keep shape"},
            )
            self.assertEqual(response.status_code, 202)
            row = self.app.state.local_engine.rows()[0]
            self.assertEqual(row["owner"], "local")
            self.assertEqual(row["prompt"], "keep shape")
            self.assertEqual(row["status"], "queued")
            self.assertTrue(Path(row["source"]).name.startswith("reference__"))

    def test_upload_rejects_fake_image(self):
        with TestClient(self.app) as client:
            response = client.post(
                "/api/local/tasks",
                files=[("files", ("fake.png", b"not-image", "image/png"))],
                data={"kind": "edit"},
            )
            self.assertEqual(response.status_code, 400)

    def test_local_process_does_not_require_cloud_credentials(self):
        (self.root / "comfy.local.json").write_text('{"enabled":true}')
        app = create_app(self.root / "config.toml")
        with patch.object(app.state.local_engine, "start"), TestClient(app) as client:
            self.assertFalse(
                client.get("/api/workspace").json()["configuration"]["setup_required"]
            )
            self.assertEqual(client.get("/objects").status_code, 200)


if __name__ == "__main__":
    unittest.main()
