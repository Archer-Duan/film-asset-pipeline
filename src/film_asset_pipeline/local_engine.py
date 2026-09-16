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

PROFILES = {
    "edit": {
        "label": "补全优化图片",
        "file": "qwen.json",
        "image": 10203,
        "prompt": 10204,
        "targets": [10205],
        "outputs": {"10205": "image"},
    },
    "model": {
        "label": "单图生成模型",
        "file": "single.json",
        "image": 343,
        "targets": [272],
        "outputs": {"272": "model"},
    },
    "full": {
        "label": "补图并生成模型",
        "file": "qwen.json",
        "image": 10203,
        "prompt": 10204,
        "targets": [10205, 272],
        "outputs": {"10205": "image", "272": "model"},
    },
    "multiview": {
        "label": "四视图拼图生成模型",
        "file": "multiview.json",
        "image": 364,
        "targets": [372],
        "outputs": {"372": "model"},
        "experimental": True,
    },
}


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

    def profile(self, kind):
        if kind not in PROFILES:
            raise ValueError("未知工作流")
        profile = {**PROFILES[kind], **self.config.get("profiles", {}).get(kind, {})}
        path = Path(profile["file"])
        if not path.is_absolute():
            path = Path(__file__).with_name("workflows") / path
        return profile, json.loads(path.read_text("utf-8"))

    def prepare(
        self,
        kind,
        image="workbench-preflight.png",
        prompt="",
        prefix="workbench/preflight",
        workflow=None,
        profile=None,
    ):
        if profile is None:
            profile, workflow = self.profile(kind)
        overrides = {(profile["image"], "image"): image}
        if prompt and profile.get("prompt"):
            overrides[(profile["prompt"], "prompt")] = prompt
        for nid, role in profile["outputs"].items():
            overrides[(int(nid), "filename_prefix")] = f"{prefix}/{role}"
        schema = self.client.request("/object_info")
        graph = compile_workflow(workflow, schema, profile["targets"], overrides)
        errors = validate_models(graph, schema)
        if errors:
            raise ValueError("；".join(errors))
        return graph

    def health(self):
        profiles = []
        for kind, base in PROFILES.items():
            try:
                self.prepare(kind)
                ready, message = True, "节点和模型配置就绪，尚未代表生成质量验收"
            except Exception as exc:
                ready, message = False, str(exc)[:500]
            profiles.append(
                {
                    "id": kind,
                    "label": base["label"],
                    "ready": ready,
                    "message": message,
                    "experimental": base.get("experimental", False),
                }
            )
        return {"enabled": self.config.get("enabled", False), "profiles": profiles}

    def submit(self, sources, kind, prompt="", owner="local", originals=None):
        if not sources or len(sources) > 50:
            raise ValueError("每批需要 1–50 张图片")
        if kind == "multiview":
            raise ValueError(
                "多视图已预留：请先配置模型及四视图裁剪区域，当前不开放提交"
            )
        profile, workflow = self.profile(kind)
        self.prepare(kind, prompt=prompt, profile=profile, workflow=workflow)
        batch = uuid.uuid4().hex
        now = utc_now()
        for index, source in enumerate(sources):
            source = Path(source).resolve()
            if not source.is_file():
                raise ValueError("输入图片不存在")
            original = (
                str(Path(originals[index]).resolve()) if originals else str(source)
            )
            identity = hashlib.sha256(
                json.dumps(
                    [sha256_file(source), kind, prompt, workflow, profile],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            with self.db() as db:
                existing = db.execute(
                    "SELECT * FROM tasks WHERE identity=? AND owner=? AND status NOT IN ('failed','interrupted') ORDER BY created DESC LIMIT 1",
                    (identity, owner),
                ).fetchone()
                if existing:
                    db.execute(
                        "INSERT OR IGNORE INTO batches VALUES(?,?)",
                        (batch, existing["id"]),
                    )
                    continue
                task_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO tasks(id,batch,identity,owner,kind,source,original,prompt,workflow,profile,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        batch,
                        identity,
                        owner,
                        kind,
                        str(source),
                        original,
                        prompt,
                        json.dumps(workflow),
                        json.dumps(profile),
                        "queued",
                        now,
                        now,
                    ),
                )
                db.execute("INSERT INTO batches VALUES(?,?)", (batch, task_id))
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
            "kind": "process-2d" if rows[0]["kind"] == "edit" else "local-3d",
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
                "name": Path(r["source"]).name,
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
            and not Path(task["source"]).is_file()
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
                image = self.client.upload(Path(task["source"]))
                graph = self.prepare(
                    task["kind"],
                    image,
                    task["prompt"],
                    f"workbench/{tid}",
                    json.loads(task["workflow"]),
                    json.loads(task["profile"]),
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
                    message="本地 GPU 计算中：" + PROFILES[task["kind"]]["label"]
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
        values = {}

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
            candidates = list(files(outputs.get(node, {})))
            suffixes = (
                (".glb",) if role == "model" else (".png", ".jpg", ".jpeg", ".webp")
            )
            file = next(
                (
                    f
                    for f in candidates
                    if str(f["filename"]).lower().endswith(suffixes)
                ),
                None,
            )
            if not file:
                raise ValueError(f"工作流完成但缺少 {role} 输出")
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
            path = (
                self.folder
                / task["id"]
                / (role + Path(file["filename"]).suffix.lower())
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
            values[role] = str(path)
        self.update(
            task["id"],
            **values,
            status="complete",
            message="生成完成，结果已归档，可审核和下载",
        )

    def manifest_rows(self):
        images, models = [], []
        for row in self.rows():
            image = row["image"] or row["source"]
            if not Path(image).is_file():
                continue
            if row["kind"] != "edit" or row["image"]:
                images.append(
                    {
                        "status": "complete",
                        "task_id": row["id"],
                        "source_path": row["original"],
                        "output_path": image,
                        "output_sha256": sha256_file(Path(image)),
                        "model": "Qwen 本地" if row["image"] else "参考图",
                        "result_index": 1,
                        "updated_at": row["created"],
                    }
                )
            if row["kind"] != "edit":
                models.append(
                    {
                        "task_id": row["id"],
                        "source_path": image,
                        "status": "complete"
                        if row["model"]
                        else "running"
                        if row["status"] not in ("failed", "interrupted", "attention")
                        else "failed",
                        "output_path": row["model"],
                        "model": "Pixal3D 本地",
                        "updated_at": row["updated"],
                    }
                )
        return images, models
