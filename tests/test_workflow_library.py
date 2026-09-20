import copy
import json
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
from film_asset_pipeline.workflow_library import parameter_values
from test_local_comfy import FakeComfy


def bundle(version="1.0.0"):
    return {
        "manifest": {
            "id": "custom-image",
            "version": version,
            "label": "Custom image",
            "inputs": [
                {
                    "key": "reference",
                    "label": "Reference",
                    "node": "1",
                    "input": "image",
                    "type": "image",
                }
            ],
            "parameters": [
                {
                    "key": "amount",
                    "label": "Amount",
                    "node": "2",
                    "input": "amount",
                    "type": "integer",
                    "min": 1,
                    "max": 10,
                    "default": 2,
                }
            ],
            "outputs": {"3": "image"},
        },
        "workflow": {
            "1": {"class_type": "LoadImage", "inputs": {"image": "sample.png"}},
            "2": {"class_type": "Effect", "inputs": {"image": ["1", 0], "amount": 2}},
            "3": {
                "class_type": "SaveImage",
                "inputs": {"images": ["2", 0], "filename_prefix": "old"},
            },
            "4": {"class_type": "MissingUnusedNode", "inputs": {}},
        },
    }


SCHEMA = {
    "LoadImage": {"input": {"required": {"image": ["STRING"]}}},
    "Effect": {"input": {"required": {"image": ["IMAGE"], "amount": ["INT"]}}},
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}
    },
}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = FakeComfy()
        self.engine = LocalEngine(self.root, self.client)
        self.image = self.root / "input.png"
        self.image.write_bytes(self.client.png)

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_compiler_overrides_prunes_and_never_mutates_original(self):
        w = bundle()["workflow"]
        before = copy.deepcopy(w)
        graph = compile_workflow(
            w, SCHEMA, ["3"], {("1", "image"): "new.png", ("2", "amount"): 7}
        )
        self.assertEqual(graph["1"]["inputs"]["image"], "new.png")
        self.assertEqual(graph["2"]["inputs"]["amount"], 7)
        self.assertNotIn("4", graph)
        self.assertEqual(w, before)
        w["1"]["inputs"]["image"] = ["3", 0]
        with self.assertRaisesRegex(ValueError, "循环"):
            compile_workflow(w, SCHEMA, ["3"], {})

    def test_unused_parameter_mapping_is_rejected(self):
        w = bundle()["workflow"]
        w["4"] = {"class_type": "LoadImage", "inputs": {"image": "unused.png"}}
        with self.assertRaisesRegex(ValueError, "未连接"):
            compile_workflow(w, SCHEMA, ["3"], {("4", "image"): "ignored.png"})

    def test_missing_model_blocks_enable_and_keeps_import_disabled(self):
        b = bundle()
        b["workflow"]["2"] = {
            "class_type": "Effect",
            "inputs": {
                "image": ["1", 0],
                "amount": 2,
                "model_name": "missing.safetensors",
            },
        }
        self.engine.library.import_bundle(b)
        schema = copy.deepcopy(SCHEMA)
        schema["Effect"]["input"]["required"]["model_name"] = [
            ["installed.safetensors"]
        ]
        with patch.object(self.client, "request", return_value=schema):
            with self.assertRaisesRegex(ValueError, "缺少模型"):
                self.engine.enable_workflow("custom-image", "1.0.0", True)
        self.assertFalse(self.engine.profile("custom-image")[0]["enabled"])

    def test_bad_binding_duplicate_key_and_path_node_rejected(self):
        for change in ("node", "input", "duplicate", "path", "output"):
            b = bundle()
            if change == "node":
                b["manifest"]["inputs"][0]["node"] = "missing"
            if change == "input":
                b["manifest"]["inputs"][0]["input"] = "missing"
            if change == "duplicate":
                b["manifest"]["inputs"] *= 2
            if change == "path":
                b["manifest"]["outputs"] = {"../../file": "image"}
            if change == "output":
                b["manifest"]["outputs"] = {"2": "image"}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.engine.library.import_bundle(b)

    def test_parameter_type_range_and_unknown_fields_rejected(self):
        p = bundle()["manifest"]
        for value in (True, 0, 11, "3", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parameter_values(p, {"amount": value})
        with self.assertRaises(ValueError):
            parameter_values(p, {"extra": 1})
        self.assertEqual(parameter_values(p, {}), {"amount": 2})

    def test_versions_disabled_on_import_and_task_snapshot_survives_update(self):
        self.engine.library.import_bundle(bundle())
        with self.assertRaisesRegex(ValueError, "未启用"):
            self.engine.submit([self.image], "custom-image")
        with patch.object(self.client, "request", return_value=SCHEMA):
            self.engine.enable_workflow("custom-image", "1.0.0", True)
            self.engine.submit([self.image], "custom-image", parameters={"amount": 4})
            second = bundle("2.0.0")
            second["workflow"]["2"]["inputs"]["amount"] = 8
            self.engine.library.import_bundle(second)
            self.engine.enable_workflow("custom-image", "2.0.0", True)
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.engine.library.import_bundle(bundle())
        restored = LocalEngine(self.root, self.client)
        row = restored.rows()[0]
        self.assertEqual(json.loads(row["profile"])["version"], "1.0.0")
        with patch.object(self.client, "request", return_value=SCHEMA):
            g = restored.prepare(
                row["kind"],
                {"reference": "restored.png"},
                workflow=json.loads(row["workflow"]),
                profile=json.loads(row["profile"]),
                parameters=json.loads(row["parameters"]),
            )
        self.assertEqual(g["2"]["inputs"]["amount"], 4)
        self.assertEqual(restored.profile("custom-image")[0]["version"], "2.0.0")
        self.assertEqual(
            sum(
                e["enabled"]
                for e in restored.library.entries()
                if e["id"] == "custom-image"
            ),
            1,
        )

    def test_all_four_views_are_one_task_and_dedupe_includes_each_view(self):
        refs = {key: self.image for key in ("front", "left", "back", "right")}
        with patch.object(self.engine, "prepare", return_value={}):
            first = self.engine.submit([], "multiview", input_sets=[refs])
            again = self.engine.submit([], "multiview", input_sets=[refs])
            self.assertEqual(first["progress"]["total"], 1)
            self.assertEqual(again["progress"]["total"], 1)
            self.assertEqual(len(self.engine.rows()), 1)
            different = self.root / "left.png"
            Image.new("RGB", (8, 8), "blue").save(different)
            refs["left"] = different
            self.engine.submit([], "multiview", input_sets=[refs])
            self.assertEqual(len(self.engine.rows()), 2)
        restored = LocalEngine(self.root, self.client)
        with (
            patch.object(restored, "prepare", return_value={}) as prepare,
            patch.object(self.client, "upload", side_effect=lambda p: p.name) as upload,
        ):
            restored.tick()
            self.assertEqual(upload.call_count, 4)
            self.assertEqual(set(prepare.call_args.args[1]), set(refs))
        self.assertEqual(len(self.client.submissions), 1)

    def test_four_view_graph_uses_individual_loaders_and_preserves_directions(self):
        profile, workflow = self.engine.profile("multiview")
        schema = {}
        for n in workflow["nodes"]:
            schema[n["type"]] = {
                "input": {
                    "optional": {f["name"]: [f["type"]] for f in n.get("inputs", [])}
                }
            }
        refs = {key: key + ".png" for key in ("front", "left", "back", "right")}
        with patch.object(self.client, "request", return_value=schema):
            graph = self.engine.prepare("multiview", refs, parameters={"fov": 35})
        expected = {"front": "338", "left": "341", "back": "342", "right": "345"}

        def sources(nid):
            if graph[nid]["class_type"] == "LoadImage":
                return {nid}
            out = set()
            for v in graph[nid]["inputs"].values():
                if (
                    isinstance(v, list)
                    and len(v) == 2
                    and isinstance(v[0], str)
                    and v[0] in graph
                ):
                    out |= sources(v[0])
            return out

        for key, node in expected.items():
            self.assertEqual(graph[node]["inputs"]["image"], key + ".png")
            self.assertEqual(sources(graph["324"]["inputs"][key][0]), {node})
        self.assertEqual(graph["324"]["inputs"]["fov"], 35)
        self.assertFalse(any(n["class_type"] == "ImageCropV2" for n in graph.values()))
        self.assertNotIn("364", graph)

    def test_missing_view_bad_parameters_and_invalid_batch_leave_no_tasks(self):
        with patch.object(self.engine, "prepare", return_value={}):
            for refs in (
                {"front": self.image},
                {k: self.image for k in ("front", "left", "back", "wrong")},
            ):
                with self.assertRaises(ValueError):
                    self.engine.submit([], "multiview", input_sets=[refs])
            with self.assertRaises(ValueError):
                self.engine.submit([self.image], "model", parameters=[1])
            with self.assertRaises(ValueError):
                self.engine.submit([self.image, self.root / "missing.png"], "model")
        self.assertEqual(self.engine.rows(), [])

    def test_multiple_image_outputs_are_archived_without_overwrite(self):
        with patch.object(self.engine, "prepare", return_value={}):
            self.engine.submit([self.image], "edit")
        row = self.engine.rows()[0]
        self.engine.collect(
            row,
            {
                "10205": {
                    "images": [{"filename": "front.png"}, {"filename": "back.png"}]
                }
            },
        )
        row = self.engine.rows()[0]
        artifacts = json.loads(row["artifacts"])
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(len({a["path"] for a in artifacts.values()}), 2)
        self.assertTrue(all(Path(a["path"]).is_file() for a in artifacts.values()))
        self.assertEqual(row["status"], "complete")
        self.assertFalse(
            any("path" in a for a in self.engine.public_tasks()[0]["artifacts"])
        )

    def test_legacy_snapshot_and_database_migration(self):
        legacy = {"label": "old", "image": 1, "targets": [3], "outputs": {"3": "image"}}
        workflow = {
            "nodes": [
                {
                    "id": 1,
                    "type": "LoadImage",
                    "inputs": [{"name": "image", "widget": {"name": "image"}}],
                    "widgets_values": ["old.png"],
                },
                {
                    "id": 3,
                    "type": "SaveImage",
                    "inputs": [
                        {"name": "images", "link": 1},
                        {
                            "name": "filename_prefix",
                            "widget": {"name": "filename_prefix"},
                        },
                    ],
                    "widgets_values": ["old"],
                },
            ],
            "links": [[1, 1, 0, 3, 0, "IMAGE"]],
        }
        with patch.object(self.client, "request", return_value=SCHEMA):
            graph = self.engine.prepare(
                "edit", "new.png", profile=legacy, workflow=workflow
            )
        self.assertEqual(graph["1"]["inputs"]["image"], "new.png")
        with self.engine.db() as db:
            for name in ("input_files", "parameters", "artifacts"):
                db.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
        LocalEngine(self.root, self.client)
        with self.engine.db() as db:
            names = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
        self.assertTrue({"input_files", "parameters", "artifacts"} <= names)


class LibraryWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.toml").write_text("", encoding="utf-8")
        self.app = create_app(self.root / "config.toml")
        self.engine = self.app.state.local_engine
        self.engine.config["enabled"] = True
        self.image = FakeComfy().png

    def tearDown(self):
        with TestClient(self.app):
            pass
        self.tmp.cleanup()

    def test_named_multipart_views_are_one_task_and_sources_remain_accessible(self):
        keys = ["right", "front", "back", "left"]
        with (
            TestClient(self.app) as client,
            patch.object(self.engine, "prepare", return_value={}),
        ):
            response = client.post(
                "/api/local/tasks",
                files=[("files", (k + ".png", self.image, "image/png")) for k in keys],
                data={
                    "kind": "multiview",
                    "input_keys": json.dumps(keys),
                    "parameters": '{"fov": 25}',
                },
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["progress"]["total"], 1)
            row = self.engine.rows()[0]
            self.assertEqual(json.loads(row["parameters"]), {"fov": 25})
            self.assertTrue(Path(row["source"]).name.startswith("front__"))
            self.assertEqual(
                client.get(f"/api/local/tasks/{row['id']}/inputs/left").content,
                self.image,
            )
            self.assertEqual(
                client.get(f"/api/local/tasks/{row['id']}/inputs/unknown").status_code,
                404,
            )

    def test_missing_and_duplicate_view_fields_rejected(self):
        with TestClient(self.app) as client:
            for keys in (["front"], ["front"] * 4):
                response = client.post(
                    "/api/local/tasks",
                    files=[("files", ("x.png", self.image)) for _ in keys],
                    data={"kind": "multiview", "input_keys": json.dumps(keys)},
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.engine.rows(), [])

    def test_admin_import_export_and_enable(self):
        with TestClient(self.app) as client:
            response = client.post(
                "/api/local/workflows/import",
                files={"file": ("bundle.json", json.dumps(bundle()).encode())},
            )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertFalse(response.json()["enabled"])
            data = client.get(
                "/api/local/workflows/custom-image/export?version=1.0.0"
            ).json()
            self.assertEqual(data["workflow"], bundle()["workflow"])
            with patch.object(self.engine.client, "request", return_value=SCHEMA):
                response = client.post(
                    "/api/local/workflows/custom-image/enabled",
                    json={"version": "1.0.0", "enabled": True},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(self.engine.profile("custom-image")[0]["enabled"])

    def test_member_cannot_import_export_or_enable(self):
        TeamAuth(self.root).add_user("artist", "long-enough-password")
        with TestClient(self.app, client=("192.168.1.20", 1234)) as client:
            client.post(
                "/api/team/login",
                json={"username": "artist", "password": "long-enough-password"},
            )
            self.assertEqual(client.get("/api/local/workflows").status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/local/workflows/import",
                    files={"file": ("x.json", json.dumps(bundle()).encode())},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.get(
                    "/api/local/workflows/model/export?version=1.1.0"
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/local/workflows/model/enabled",
                    json={"version": "1.1.0", "enabled": False},
                ).status_code,
                403,
            )


if __name__ == "__main__":
    unittest.main()
