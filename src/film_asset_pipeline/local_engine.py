"""Durable, single-GPU queue shared by the web app and CLI processes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .comfy import ComfyClient, compile_workflow, validate_models
from .pipeline import atomic_write, sha256_file
from .state import utc_now
from .asset_files import task_prefix, result_relative_path

from .workflow_library import WorkflowLibrary, parameter_values


def local_config(root):
    path = Path(root) / "comfy.local.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {"enabled": False}


class LocalEngine:
    def __init__(self, root, client=None):
        self.root = Path(root)
        self.folder = self.root / "data/local-assets"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.folder / "tasks.sqlite3"
        self.config = local_config(root)
        self.client = client or ComfyClient(
            self.config.get("url", "http://127.0.0.1:8188")
        )
        self.library = WorkflowLibrary(root)
        self.stop = threading.Event()
        self.thread = None
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, batch TEXT NOT NULL, identity TEXT NOT NULL,
                owner TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                original TEXT NOT NULL, prompt TEXT NOT NULL, workflow TEXT NOT NULL,
                profile TEXT NOT NULL, status TEXT NOT NULL, remote_id TEXT,
                image TEXT, model TEXT, message TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL, updated TEXT NOT NULL)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            for name in ("input_files", "parameters", "artifacts", "title"):
                if name not in columns:
                    try:
                        db.execute(
                            f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT '{{}}'"
                        )
                    except sqlite3.OperationalError:
                        if name not in {
                            r[1] for r in db.execute("PRAGMA table_info(tasks)")
                        }:
                            raise
            db.execute("UPDATE tasks SET title='' WHERE title='{}'")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_identity ON tasks(identity)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS batches(batch TEXT,task TEXT,PRIMARY KEY(batch,task))"
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def profile(self, kind, version=None):
        return self.library.load(kind, version, self.config)

    def prepare(
        self,
        kind,
        image="workbench-preflight.png",
        prompt="",
        prefix="workbench/preflight",
        workflow=None,
        profile=None,
        parameters=None,
    ):
        if profile is None:
            profile, workflow = self.profile(kind)
        # In-flight v1.0 tasks retain their original frozen profile.
        if "inputs" not in profile:
            overrides = {(profile["image"], "image"): image}
            if prompt and profile.get("prompt"):
                overrides[(profile["prompt"], "prompt")] = prompt
        else:
            supplied = dict(parameters or {})
            if prompt and any(f["key"] == "prompt" for f in profile["parameters"]):
                supplied["prompt"] = prompt
            values = parameter_values(profile, supplied)
            images = (
                image
                if isinstance(image, dict)
                else {f["key"]: image for f in profile["inputs"]}
            )
            ui = "nodes" in workflow

            def nid(value):
                return int(value) if ui else str(value)

            overrides = {
                (nid(f["node"]), f["input"]): images[f["key"]]
                for f in profile["inputs"]
            }
            for f in profile["parameters"]:
                if f["key"] in values and not (
                    f.get("omit_empty") and values[f["key"]] == ""
                ):
                    overrides[(nid(f["node"]), f["input"])] = values[f["key"]]
        for node, role in profile["outputs"].items():
            overrides[
                (int(node) if "nodes" in workflow else str(node), "filename_prefix")
            ] = f"{prefix.get(role) if isinstance(prefix, dict) else prefix}__{node}-{role}"
        schema = self.client.request("/object_info")
        graph = compile_workflow(workflow, schema, profile["targets"], overrides)
        # Intermediate image-saving nodes also need the task/date prefix.
        if isinstance(prefix, dict):
            for node_id, node in graph.items():
                if "filename_prefix" in node["inputs"]:
                    role = profile["outputs"].get(node_id, "image")
                    raw = (
                        str(node["inputs"]["filename_prefix"])
                        .replace("\\", "/")
                        .rsplit("/", 1)[-1]
                    )
                    label = {
                        "front_view": "正面",
                        "left_view": "左侧",
                        "back_view": "背面",
                        "right_view": "右侧",
                    }.get(raw, "模型" if role == "model" else "图片")
                    node["inputs"]["filename_prefix"] = (
                        f"{prefix[role]}__{label}-{node_id}"
                    )
        errors = validate_models(graph, schema)
        if errors:
            raise ValueError("；".join(errors))
        return graph

    def health(self):
        profiles = []
        for entry in self.library.entries():
            try:
                profile, workflow = self.profile(entry["id"], entry["version"])
                self.prepare(entry["id"], profile=profile, workflow=workflow)
                ready, message = True, "节点和模型配置就绪，尚未代表生成质量验收"
            except Exception as exc:
                profile = entry
                ready, message = False, str(exc)[:500]
            profiles.append(
                {
                    k: profile.get(k)
                    for k in (
                        "id",
                        "version",
                        "label",
                        "description",
                        "inputs",
                        "parameters",
                        "outputs",
                        "builtin",
                        "enabled",
                    )
                }
                | {"ready": ready, "message": message}
            )
        return {"enabled": self.config.get("enabled", False), "profiles": profiles}

    def enable_workflow(self, kind, version, enabled):
        profile, workflow = self.profile(kind, version)
        if enabled:
            self.prepare(kind, profile=profile, workflow=workflow)
        self.library.set_enabled(kind, version, enabled)

    def submit(
        self,
        sources,
        kind,
        prompt="",
        owner="local",
        originals=None,
        *,
        input_sets=None,
        parameters=None,
        version=None,
        title="",
        titles=None,
    ):
        if not isinstance(title, str) or len(title) > 80:
            raise ValueError("资产名称最多 80 字符")
        title = title.strip()
        titles = titles or {}
        if any(not isinstance(v, str) or len(v) > 80 for v in titles.values()):
            raise ValueError("资产名称最多 80 字符")
        profile, workflow = self.profile(kind, version)
        if not profile.get("enabled"):
            raise ValueError("该工作流版本未启用")
        fields = profile["inputs"]
        if input_sets is None:
            if len(fields) != 1:
                raise ValueError(
                    "该工作流需要按名称提供多张视图，不能把每张图作为独立任务"
                )
            input_sets = [{fields[0]["key"]: source} for source in sources]
        if not input_sets or len(input_sets) > 50:
            raise ValueError("每批需要 1–50 项任务")
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError("参数需要 JSON 对象")
        supplied = dict(parameters or {})
        if prompt:
            if not any(f["key"] == "prompt" for f in profile["parameters"]):
                raise ValueError("该工作流不支持提示词")
            supplied["prompt"] = prompt
        values = parameter_values(profile, supplied)
        prepared = []
        from PIL import Image

        for mapping in input_sets:
            if set(mapping) != {f["key"] for f in fields}:
                raise ValueError("图片输入不完整或包含未声明的视图")
            files = {f["key"]: str(Path(mapping[f["key"]]).resolve()) for f in fields}
            for source in files.values():
                path = Path(source)
                if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("输入图片不存在或超过 20MB")
                with Image.open(path) as im:
                    if (
                        im.format not in ("PNG", "JPEG", "WEBP")
                        or im.width * im.height > 40_000_000
                    ):
                        raise ValueError("需要 JPG、PNG 或 WebP，最多 4000 万像素")
                    im.verify()
            prepared.append(files)
        self.prepare(
            kind, prompt=prompt, profile=profile, workflow=workflow, parameters=values
        )
        batch, now = uuid.uuid4().hex, utc_now()
        # Entire batch is atomic; a bad later image cannot leave earlier jobs queued.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for index, files in enumerate(prepared):
                source = files[fields[0]["key"]]
                original = (
                    str(Path(originals[index]).resolve()) if originals else source
                )
                identity = hashlib.sha256(
                    json.dumps(
                        [
                            {
                                key: sha256_file(Path(path))
                                for key, path in files.items()
                            },
                            kind,
                            values,
                            workflow,
                            profile,
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                existing = db.execute(
                    "SELECT id FROM tasks WHERE identity=? AND owner=? AND status NOT IN ('failed','interrupted') ORDER BY created DESC LIMIT 1",
                    (identity, owner),
                ).fetchone()
                if existing:
                    task_id = existing["id"]
                else:
                    task_id = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO tasks(id,batch,identity,owner,kind,source,original,prompt,workflow,profile,status,created,updated,input_files,parameters) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            task_id,
                            batch,
                            identity,
                            owner,
                            kind,
                            source,
                            original,
                            str(values.get("prompt", prompt)),
                            json.dumps(workflow),
                            json.dumps(profile),
                            "queued",
                            now,
                            now,
                            json.dumps(files),
                            json.dumps(values),
                        ),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO batches VALUES(?,?)", (batch, task_id)
                )
                if not existing:
                    db.execute(
                        "UPDATE tasks SET title=? WHERE id=?",
                        (titles.get(source, title).strip(), task_id),
                    )
        return self.group(batch)

    def rows(self):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM tasks ORDER BY created DESC")
            ]

    def update(self, task_id, **values):
        values["updated"] = utc_now()
        with self.db() as db:
            db.execute(
                "UPDATE tasks SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=?",
                [*values.values(), task_id],
            )

    def group(self, batch):
        with self.db() as db:
            ids = {
                r[0]
                for r in db.execute("SELECT task FROM batches WHERE batch=?", (batch,))
            }
        rows = [r for r in self.rows() if r["id"] in ids or r["batch"] == batch]
        if not rows:
            raise KeyError(batch)
        complete = sum(r["status"] == "complete" for r in rows)
        failed = sum(
            r["status"] in ("failed", "interrupted", "attention") for r in rows
        )
        status = (
            "complete"
            if complete == len(rows)
            else "failed"
            if complete + failed == len(rows)
            else "running"
        )
        if all(r["status"] == "queued" for r in rows):
            status = "queued"
        active = next(
            (r for r in rows if r["status"] not in ("complete", "failed")), rows[0]
        )
        return {
            "job_id": batch,
            "kind": "process-2d"
            if "model" not in json.loads(rows[0]["profile"])["outputs"].values()
            else "local-3d",
            "status": status,
            "message": active["message"] or f"排队中，共 {len(rows)} 项",
            "progress": {
                "total": len(rows),
                "completed": complete + failed,
                "succeeded": complete,
                "failed": failed,
                "percent": round((complete + failed) / len(rows) * 100),
            },
        }

    def public_tasks(self):
        return [
            {
                k: r[k]
                for k in (
                    "id",
                    "batch",
                    "owner",
                    "kind",
                    "status",
                    "message",
                    "created",
                    "updated",
                    "prompt",
                )
            }
            | {
                "name": r["title"] or Path(r["source"]).stem.split("__", 1)[0],
                "title": r["title"],
                "label": json.loads(r["profile"]).get("label", r["kind"]),
                "version": json.loads(r["profile"]).get("version", "1.0.0"),
                "inputs": [
                    {"key": f["key"], "label": f["label"]}
                    for f in json.loads(r["profile"]).get("inputs", [])
                ],
                "parameters": json.loads(r["parameters"]),
                "artifacts": [
                    {k: a[k] for k in ("id", "role", "name")}
                    for a in json.loads(r["artifacts"]).values()
                ],
                "has_image": bool(r["image"]),
                "has_model": bool(r["model"]),
            }
            for r in self.rows()
        ]

    def retry(self, task_id, owner):
        row = next((r for r in self.rows() if r["id"] == task_id), None)
        if not row or owner not in (row["owner"], "local"):
            raise ValueError("任务不存在或无权重试")
        if row["status"] not in ("failed", "interrupted", "waiting"):
            raise ValueError("该任务不需要重试；提交状态不确定的任务请由管理员核查")
        self.update(
            task_id,
            status="queued" if not row["remote_id"] else "submitted",
            message="等待重试",
        )
        return self.group(row["batch"])

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(
            target=self.work, daemon=True, name="comfy-worker"
        )
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def work(self):
        # OS lock is released on process death. Web/CLI can both enqueue, only one executes.
        with (self.folder / "worker.lock").open("a+b") as lock:
            lock.seek(0)
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            while not self.stop.is_set():
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self.stop.wait(2)
                    continue
                try:
                    self.tick()
                except Exception:
                    # A single transport/SQLite failure must not kill the worker.
                    import logging

                    logging.getLogger(__name__).exception(
                        "Local worker iteration failed"
                    )
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                self.stop.wait(3)

    def tick(self):
        rows = list(reversed(self.rows()))
        active = next(
            (
                r
                for r in rows
                if r["status"] in ("submitting", "submitted", "running", "waiting")
            ),
            None,
        )
        task = active or next((r for r in rows if r["status"] == "queued"), None)
        if not task:
            return
        tid = task["id"]
        if (
            not task["remote_id"]
            and task["status"] == "queued"
            and not all(
                Path(p).is_file()
                for p in (
                    json.loads(task["input_files"]) or {"image": task["source"]}
                ).values()
            )
        ):
            self.update(tid, status="failed", message="输入图片已不存在，请重新上传")
            return
        try:
            if not task["remote_id"]:
                if task["status"] == "submitting":
                    # The POST may have succeeded before its response/DB write was lost.
                    history = self.client.request("/history")
                    queue = self.client.request("/queue")
                    prompts = (
                        [h.get("prompt", []) for h in history.values()]
                        + queue.get("queue_running", [])
                        + queue.get("queue_pending", [])
                    )
                    match = next(
                        (
                            p
                            for p in prompts
                            if len(p) > 3 and p[3].get("workbench_task") == tid
                        ),
                        None,
                    )
                    if match:
                        self.update(
                            tid,
                            remote_id=match[1],
                            status="submitted",
                            message="已恢复 ComfyUI 任务关联",
                        )
                    else:
                        self.update(
                            tid,
                            status="attention",
                            message="提交结果不确定，已停止自动重发；请管理员核对 ComfyUI 历史",
                        )
                    return
                # Respect tasks submitted directly in ComfyUI as well.
                queue = self.client.request("/queue")
                if queue.get("queue_running") or queue.get("queue_pending"):
                    self.update(tid, message="等待 ComfyUI 当前任务完成")
                    return
                input_files = json.loads(task["input_files"])
                image = (
                    {
                        key: self.client.upload(Path(path))
                        for key, path in input_files.items()
                    }
                    if input_files
                    else self.client.upload(Path(task["source"]))
                )
                graph = self.prepare(
                    task["kind"],
                    image,
                    task["prompt"],
                    {role: task_prefix(task, role) for role in ("image", "model")},
                    json.loads(task["workflow"]),
                    json.loads(task["profile"]),
                    parameters=json.loads(task["parameters"]),
                )
                self.update(tid, status="submitting", message="正在提交到本地 ComfyUI")
                response = self.client.request(
                    "/prompt",
                    {
                        "prompt": graph,
                        "client_id": f"workbench-{tid}",
                        "extra_data": {"workbench_task": tid},
                    },
                )
                if response.get("node_errors") or not response.get("prompt_id"):
                    self.update(
                        tid,
                        status="failed",
                        message="工作流校验失败："
                        + str(response.get("node_errors", response))[:1200],
                    )
                    return
                self.update(
                    tid,
                    remote_id=response["prompt_id"],
                    status="submitted",
                    message="已提交，等待本地计算",
                )
                return
            remote = task["remote_id"]
            history = self.client.request("/history/" + remote)
            if remote in history:
                item = history[remote]
                if item.get("status", {}).get("status_str") == "error":
                    error = next(
                        (
                            m[1]
                            for m in item.get("status", {}).get("messages", [])
                            if m[0] in ("execution_error", "execution_interrupted")
                        ),
                        {},
                    )
                    self.update(
                        tid,
                        status="failed",
                        remote_id=None,
                        message=f"节点 {error.get('node_type', '')}：{error.get('exception_message', '执行中断')}"[
                            :1500
                        ],
                    )
                    return
                if item.get("status", {}).get("completed"):
                    self.collect(task, item.get("outputs", {}))
                    return
            queue = self.client.request("/queue")
            running = any(p[1] == remote for p in queue.get("queue_running", []))
            pending = any(p[1] == remote for p in queue.get("queue_pending", []))
            if not running and not pending:
                self.update(
                    tid,
                    status="interrupted",
                    remote_id=None,
                    message="ComfyUI 中已找不到任务，可能后端重启或历史被清除；可手动重试",
                )
            else:
                self.update(
                    tid,
                    status="running" if running else "submitted",
                    message="本地 GPU 计算中：" + json.loads(task["profile"])["label"]
                    if running
                    else "ComfyUI 排队中",
                )
        except (OSError, ValueError, KeyError) as exc:
            # Never silently repeat an uncertain POST; submitting is reconciled next tick.
            current = next(r for r in self.rows() if r["id"] == tid)
            status = current["status"]
            if status == "queued":
                status = "failed" if isinstance(exc, ValueError) else "queued"
            elif current["remote_id"]:
                status = "waiting"
            self.update(
                tid,
                status=status,
                message=("等待连接恢复：" if isinstance(exc, OSError) else "")
                + str(exc)[:1200],
            )

    def collect(self, task, outputs):
        profile = json.loads(task["profile"])
        values, artifacts = {}, {}

        def files(value):
            if isinstance(value, dict):
                if "filename" in value:
                    yield value
                for child in value.values():
                    yield from files(child)
            elif isinstance(value, list):
                for child in value:
                    yield from files(child)

        for node, role in profile["outputs"].items():
            suffixes = (
                (".glb",) if role == "model" else (".png", ".jpg", ".jpeg", ".webp")
            )
            candidates = []
            seen = set()
            for file in files(outputs.get(node, {})):
                identity = (
                    file.get("filename"),
                    file.get("subfolder"),
                    file.get("type"),
                )
                if (
                    str(file["filename"]).lower().endswith(suffixes)
                    and identity not in seen
                ):
                    seen.add(identity)
                    candidates.append(file)
            if not candidates:
                raise ValueError(f"工作流完成但缺少 {role} 输出")
            for index, file in enumerate(candidates):
                data = self.client.download(file)
                if role == "model":
                    if (
                        len(data) < 20
                        or data[:4] != b"glTF"
                        or struct.unpack_from("<II", data, 4) != (2, len(data))
                    ):
                        raise ValueError("模型不是有效 GLB 2.0 文件")
                else:
                    from PIL import Image
                    from io import BytesIO

                    with Image.open(BytesIO(data)) as im:
                        im.verify()
                artifact_id = f"a{len(artifacts)}"
                relative = result_relative_path(
                    task, role, len(artifacts), Path(file["filename"]).suffix
                )
                name = relative.name
                path = self.folder / "results" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, data)
                artifacts[artifact_id] = {
                    "id": artifact_id,
                    "role": role,
                    "name": name,
                    "path": str(path),
                }
                values.setdefault(role, str(path))
        self.update(
            task["id"],
            **values,
            artifacts=json.dumps(artifacts),
            status="complete",
            message="生成完成，结果已归档，可审核和下载",
        )

    def source_rows(self):
        results = []
        rows = self.rows()
        generated = {
            a["path"]
            for r in rows
            for a in json.loads(r["artifacts"]).values()
            if a["role"] == "image"
        } | {r["image"] for r in rows if r["image"]}
        for row in rows:
            profile = json.loads(row["profile"])
            files = json.loads(row["input_files"]) or {"image": row["source"]}
            labels = {f["key"]: f["label"] for f in profile.get("inputs", [])}
            has_result = bool(row["image"] or row["model"])
            state = (
                "processed"
                if has_result
                else "failed"
                if row["status"] in ("failed", "interrupted", "attention")
                else "processing"
            )
            # A model-only task consumes an existing image, it does not run image generation.
            if (
                "model" in profile["outputs"].values()
                and "image" not in profile["outputs"].values()
                and state == "processing"
            ):
                state = "processed"
            paths = dict(files)
            if row["original"] not in paths.values():
                paths["original"] = row["original"]
            for key, path in paths.items():
                if path in generated:
                    continue
                results.append(
                    {
                        "path": path,
                        "title": (
                            row["title"]
                            + (
                                " · " + labels.get(key, "原图")
                                if len(files) > 1
                                else ""
                            )
                        )
                        if row["title"]
                        else "",
                        "status": state,
                        "task_id": row["id"],
                        "message": row["message"],
                        "updated_at": row["updated"],
                        "processed": has_result or state == "processed",
                    }
                )
        return results

    def manifest_rows(self):
        images, models = [], []
        for row in self.rows():
            profile = json.loads(row["profile"])
            has_model = "model" in profile["outputs"].values()
            artifacts = list(json.loads(row["artifacts"]).values())
            image_files = [a["path"] for a in artifacts if a["role"] == "image"] or (
                [row["image"]] if row["image"] else []
            )
            if has_model and not image_files:
                image_files = [row["source"]]
            for index, image in enumerate(dict.fromkeys(image_files)):
                if not Path(image).is_file():
                    continue
                images.append(
                    {
                        "status": "complete",
                        "task_id": row["id"],
                        "source_path": row["original"],
                        "source_paths": list(
                            dict.fromkeys(
                                [
                                    row["original"],
                                    *(json.loads(row["input_files"]) or {}).values(),
                                ]
                            )
                        ),
                        "output_path": image,
                        "output_sha256": sha256_file(Path(image)),
                        "model": profile.get("label", "本地工作流")
                        if row["image"]
                        else "参考图",
                        "title": row["title"],
                        "result_index": index + 1,
                        "updated_at": row["created"],
                        "keep_history": True,
                    }
                )
            first_image_row = next(
                (r for r in images if r["task_id"] == row["id"]), None
            )
            if has_model and image_files and Path(image_files[0]).is_file():
                models.append(
                    {
                        "task_id": row["id"],
                        "source_path": image_files[0],
                        "status": "complete"
                        if row["model"]
                        else "failed"
                        if row["status"] in ("failed", "interrupted", "attention")
                        else "running",
                        "error_message": row["message"],
                        "output_path": row["model"],
                        "model": profile.get("label", "本地工作流"),
                        "updated_at": row["updated"],
                    }
                )
            extra_models = [
                a
                for a in artifacts
                if a["role"] == "model"
                and a["path"] != row["model"]
                and Path(a["path"]).is_file()
            ]
            for index, artifact in enumerate(extra_models, start=2):
                if first_image_row is None:
                    continue
                images.append(
                    first_image_row
                    | {
                        "output_sha256": hashlib.sha256(
                            f"{first_image_row['output_sha256']}:{row['id']}:{artifact['id']}".encode()
                        ).hexdigest(),
                        "model_artifact_id": artifact["id"],
                        "title": f"{row['title'] or Path(row['source']).stem.split('__', 1)[0]} · 模型 {index}",
                    }
                )
                models.append(
                    {
                        "task_id": row["id"],
                        "artifact_id": artifact["id"],
                        "source_path": first_image_row["output_path"],
                        "status": "complete",
                        "output_path": artifact["path"],
                        "updated_at": row["updated"],
                    }
                )
        return images, models
