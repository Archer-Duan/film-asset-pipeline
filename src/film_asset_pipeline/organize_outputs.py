"""Explicit, restartable migration of workbench-owned output files.

Run with the web workers stopped. Unrelated ComfyUI outputs are never selected.
The journal and SQLite backup are kept in data/local-assets/organization.
"""

from contextlib import closing
import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from .asset_files import result_relative_path, task_prefix
from .local_engine import LocalEngine
from .pipeline import atomic_write, sha256_file


def history_files(value):
    if isinstance(value, dict):
        if "filename" in value:
            yield value
        for child in value.values():
            yield from history_files(child)
    elif isinstance(value, list):
        for child in value:
            yield from history_files(child)


def contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"文件超出指定目录：{path}")
    return path


def plan_organization(engine, output_root):
    output_root = Path(output_root).resolve()
    rows = engine.rows()
    if any(r["status"] not in ("complete", "failed", "interrupted") for r in rows):
        raise ValueError("请等待所有任务结束并停止工作台服务后整理")
    moves, cached = {}, {}
    for task in rows:
        artifacts = json.loads(task["artifacts"])
        files = [(a["path"], a["role"]) for a in artifacts.values()]
        files.extend((task[role], role) for role in ("image", "model") if task[role])
        for index, (file, role) in enumerate(dict.fromkeys(files)):
            source = contained(file, engine.folder)
            target = (
                engine.folder
                / "results"
                / result_relative_path(task, role, index, source.suffix)
            )
            if source != target.resolve() and source.is_file():
                cached[str(source)] = str(target.resolve())
        owned = output_root / "workbench" / task["id"]
        candidates = set(owned.rglob("*")) if owned.is_dir() else set()
        if task["remote_id"]:
            history = engine.client.request("/history/" + task["remote_id"])
            item = history.get(task["remote_id"], {})
            for file in history_files(item.get("outputs", {})):
                if file.get("type", "output") != "output":
                    continue
                path = contained(
                    output_root / file.get("subfolder", "") / file["filename"],
                    output_root,
                )
                # New date-organized paths need no migration.
                if path.parent == output_root or path.is_relative_to(owned):
                    candidates.add(path)
        for index, file in enumerate(sorted(candidates)):
            if not file.is_file() or file.suffix.lower() not in (
                ".glb",
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            ):
                continue
            source = contained(file, output_root)
            role = "model" if file.suffix.lower() == ".glb" else "image"
            view = next(
                (
                    label
                    for name, label in {
                        "front_view": "正面",
                        "left_view": "左侧",
                        "back_view": "背面",
                        "right_view": "右侧",
                    }.items()
                    if file.name.startswith(name)
                ),
                "模型" if role == "model" else "图片",
            )
            target = contained(
                output_root
                / f"{task_prefix(task, role)}__{view}-{index + 1:02d}{file.suffix.lower()}",
                output_root,
            )
            if str(source) in moves and moves[str(source)] != str(target):
                raise ValueError("历史文件对应多个任务，请人工核对后再整理")
            moves[str(source)] = str(target)
    return {"cached": cached, "native": moves}


def apply_organization(engine, output_root, plan):
    output_root = Path(output_root).resolve()
    folder = engine.folder / "organization"
    folder.mkdir(exist_ok=True)
    journal = folder / "paths.json"
    previous = (
        json.loads(journal.read_text("utf-8"))
        if journal.exists()
        else {"cached": {}, "native": {}}
    )
    combined = {key: previous[key] | plan[key] for key in ("cached", "native")}
    # Journal first, so a crash can always be retried using the exact same names.
    atomic_write(
        journal, json.dumps(combined, ensure_ascii=False, indent=2).encode("utf-8")
    )
    backup = folder / "tasks-before.sqlite3"
    if not backup.exists():
        with (
            closing(sqlite3.connect(engine.db_path)) as source,
            closing(sqlite3.connect(backup)) as target,
        ):
            source.backup(target)
    for key, root in (("cached", engine.folder), ("native", output_root)):
        for old, new in combined[key].items():
            source, target = contained(old, root), contained(new, root)
            if not source.exists():
                if not target.is_file():
                    raise ValueError(f"整理文件已丢失：{source}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            if sha256_file(source) != sha256_file(target):
                raise ValueError(f"目标文件内容不一致，未替换原文件：{target}")
    replacements = combined["cached"]

    def mapped(value):
        return replacements.get(str(Path(value).resolve()), value) if value else value

    with engine.db() as db:
        for task in engine.rows():
            artifacts = json.loads(task["artifacts"])
            for artifact in artifacts.values():
                artifact["path"] = mapped(artifact["path"])
                artifact["name"] = Path(artifact["path"]).name
            db.execute(
                "UPDATE tasks SET source=?,original=?,image=?,model=?,input_files=?,artifacts=? WHERE id=?",
                [
                    *[
                        mapped(task[k])
                        for k in ("source", "original", "image", "model")
                    ],
                    json.dumps(
                        {
                            k: mapped(v)
                            for k, v in json.loads(task["input_files"]).items()
                        }
                    ),
                    json.dumps(artifacts),
                    task["id"],
                ],
            )
    # Keep old local cache files as rollback copies. Remove native originals only
    # after verified copies and database updates; never recursively remove folders.
    for old, new in combined["native"].items():
        source, target = contained(old, output_root), contained(new, output_root)
        if source.exists():
            if sha256_file(source) != sha256_file(target):
                raise ValueError("整理期间文件发生变化，已停止清理")
            source.unlink()
            parent = source.parent
            while parent != output_root and parent.is_relative_to(output_root):
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
    return {key: len(combined[key]) for key in combined}


def main():
    parser = argparse.ArgumentParser(description="按日期整理工作台历史输出，默认仅预览")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = LocalEngine(args.root)
    plan = plan_organization(engine, args.output_root)
    result = apply_organization(engine, args.output_root, plan) if args.apply else plan
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
