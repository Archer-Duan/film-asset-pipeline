"""Versioned, declarative workflow bundles; importing never executes a workflow."""

import copy
import json
import math
import re
import sqlite3
from pathlib import Path

from .state import utc_now


def validate_manifest(manifest, workflow):
    if not isinstance(manifest, dict) or not isinstance(workflow, dict):
        raise ValueError("需要 manifest 和 workflow 对象")
    m = copy.deepcopy(manifest)
    for key in ("id", "version"):
        if not isinstance(m.get(key), str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", m[key]
        ):
            raise ValueError(
                f"{key} 仅允许字母、数字、点、下划线和短横线，最多 64 字符"
            )
    if not isinstance(m.get("label"), str) or not 1 <= len(m["label"]) <= 100:
        raise ValueError("工作流名称需要 1–100 个字符")
    if (
        not isinstance(m.get("description", ""), str)
        or len(m.get("description", "")) > 2000
    ):
        raise ValueError("说明最多 2000 字符")
    images, params = m.get("inputs"), m.get("parameters", [])
    if (
        not isinstance(images, list)
        or not 1 <= len(images) <= 8
        or not isinstance(params, list)
        or len(params) > 24
    ):
        raise ValueError("支持 1–8 个图片输入和最多 24 个参数")
    ui = isinstance(workflow.get("nodes"), list)
    try:
        nodes = {str(n["id"]): n for n in workflow["nodes"]} if ui else workflow
        keys, bindings = set(), set()
        for f in images + params:
            key = f["key"]
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key)
                or key in keys
            ):
                raise ValueError("输入名称必须唯一，使用小写字母、数字和下划线")
            keys.add(key)
            if not isinstance(f.get("label"), str) or not 1 <= len(f["label"]) <= 100:
                raise ValueError("每个输入需要显示名称")
            node_id, field = str(f["node"]), f["input"]
            if not re.fullmatch(r"[A-Za-z0-9_]+", node_id) or not isinstance(
                field, str
            ):
                raise ValueError("节点 ID 或输入名称不合法")
            node = nodes[node_id]
            names = (
                {i["name"] for i in node.get("inputs", [])}
                if ui
                else set(node["inputs"])
            )
            if field not in names or (node_id, field) in bindings:
                raise ValueError(f"输入映射不存在或重复：{node_id}.{field}")
            bindings.add((node_id, field))
            if f in images:
                if f.get("type") != "image":
                    raise ValueError("图片输入 type 必须是 image")
            elif f.get("type") not in (
                "text",
                "integer",
                "number",
                "boolean",
                "choice",
            ):
                raise ValueError("不支持的参数类型")
            elif f["type"] == "choice" and (
                not isinstance(f.get("choices"), list)
                or not f["choices"]
                or not all(isinstance(x, str) for x in f["choices"])
            ):
                raise ValueError("选择参数需要非空字符串 choices")
            for limit in ("min", "max"):
                if limit in f and (
                    type(f[limit]) not in (int, float) or not math.isfinite(f[limit])
                ):
                    raise ValueError("参数范围必须是有限数值")
            if f.get("min", -float("inf")) > f.get("max", float("inf")):
                raise ValueError("参数范围上下限不正确")
        outputs = m["outputs"]
        if not isinstance(outputs, dict) or not 1 <= len(outputs) <= 8:
            raise ValueError("需要 1–8 个输出节点")
        for node_id, role in outputs.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_]+", str(node_id))
                or str(node_id) not in nodes
                or role not in ("image", "model")
            ):
                raise ValueError("输出节点不存在，或输出类型不是 image/model")
            output = nodes[str(node_id)]
            names = (
                {i["name"] for i in output.get("inputs", [])}
                if ui
                else set(output["inputs"])
            )
            if "filename_prefix" not in names:
                raise ValueError(
                    "输出节点需要 filename_prefix 输入以隔离每个任务的文件"
                )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(
            "工作流或映射结构不完整，请检查 node、input 和 outputs"
        ) from exc
    m["parameters"] = params
    m["targets"] = [int(n) if ui else str(n) for n in outputs]
    parameter_values(m, {})
    return m


def parameter_values(profile, supplied):
    if not isinstance(supplied, dict):
        raise ValueError("参数需要 JSON 对象")
    fields = {f["key"]: f for f in profile.get("parameters", [])}
    if set(supplied) - set(fields):
        raise ValueError("存在未声明的工作流参数")
    result = {}
    for key, f in fields.items():
        value = supplied.get(key, f.get("default"))
        if value is None:
            if f.get("required"):
                raise ValueError(f"请填写 {f['label']}")
            continue
        typ = f["type"]
        if typ == "text":
            valid = (
                isinstance(value, str)
                and len(value) <= 8000
                and (bool(value.strip()) or not f.get("required"))
            )
        elif typ == "boolean":
            valid = type(value) is bool
        elif typ == "choice":
            valid = isinstance(value, str) and value in f["choices"]
        else:
            valid = (
                type(value) is int if typ == "integer" else type(value) in (int, float)
            )
            valid = (
                valid
                and (type(value) is int or math.isfinite(value))
                and f.get("min", -float("inf")) <= value <= f.get("max", float("inf"))
            )
        if not valid:
            raise ValueError(f"参数 {f['label']} 的类型或范围不正确")
        result[key] = value
    return result


class WorkflowLibrary:
    def __init__(self, root):
        self.base = Path(__file__).with_name("workflows")
        self.builtins = json.loads((self.base / "catalog.json").read_text("utf-8"))
        self.path = Path(root) / "data/local-assets/workflows.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bundles(id TEXT,version TEXT,bundle TEXT,enabled INTEGER DEFAULT 0,created TEXT,PRIMARY KEY(id,version))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS switches(id TEXT PRIMARY KEY,enabled INTEGER)"
            )

    def connect(self):
        # contextlib.closing alone would not commit; explicitly close after transactions.
        from contextlib import contextmanager

        @contextmanager
        def transaction():
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

        return transaction()

    def entries(self):
        with self.connect() as db:
            switches = dict(db.execute("SELECT id,enabled FROM switches"))
            custom = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM bundles ORDER BY created DESC,rowid DESC"
                )
            ]
        entries = [
            {
                **copy.deepcopy(m),
                "builtin": True,
                "enabled": bool(switches.get(m["id"], 1)),
            }
            for m in self.builtins
        ]
        for r in custom:
            entries.append(
                {
                    **json.loads(r["bundle"])["manifest"],
                    "builtin": False,
                    "enabled": bool(r["enabled"]),
                }
            )
        return entries

    def load(self, kind, version=None, config=None):
        entries = [
            m
            for m in self.entries()
            if m["id"] == kind and (version is None or m["version"] == version)
        ]
        if not entries:
            raise ValueError("未知工作流或版本")
        m = next((e for e in entries if e["enabled"]), entries[0])
        if m["builtin"]:
            m.update((config or {}).get("profiles", {}).get(kind, {}))
            path = Path(m["file"])
            if not path.is_absolute():
                path = self.base / path
            workflow = json.loads(path.read_text("utf-8"))
            # Compatibility with v1.0 local node mappings.
            if "image" in m:
                m["inputs"][0]["node"] = m["image"]
            if "prompt" in m:
                for f in m["parameters"]:
                    if f["key"] == "prompt":
                        f["node"] = m["prompt"]
        else:
            with self.connect() as db:
                r = db.execute(
                    "SELECT bundle FROM bundles WHERE id=? AND version=?",
                    (kind, m["version"]),
                ).fetchone()
            workflow = json.loads(r[0])["workflow"]
        return validate_manifest(m, workflow), workflow

    def import_bundle(self, bundle):
        if not isinstance(bundle, dict):
            raise ValueError("工作流包需要 JSON 对象")
        m = validate_manifest(bundle.get("manifest"), bundle.get("workflow"))
        if m["id"] in {b["id"] for b in self.builtins}:
            raise ValueError("请使用新的工作流 ID，不能覆盖内置工作流")
        for key in ("enabled", "builtin", "file", "image", "prompt"):
            m.pop(key, None)
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO bundles(id,version,bundle,created) VALUES(?,?,?,?)",
                    (
                        m["id"],
                        m["version"],
                        json.dumps({"manifest": m, "workflow": bundle["workflow"]}),
                        utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该版本已存在；修改工作流请使用新版本号") from exc
        return m

    def set_enabled(self, kind, version, enabled):
        m, _ = self.load(kind, version)
        with self.connect() as db:
            if m["builtin"]:
                db.execute(
                    "INSERT OR REPLACE INTO switches VALUES(?,?)", (kind, int(enabled))
                )
            else:
                if enabled:
                    db.execute("UPDATE bundles SET enabled=0 WHERE id=?", (kind,))
                db.execute(
                    "UPDATE bundles SET enabled=? WHERE id=? AND version=?",
                    (int(enabled), kind, version),
                )
