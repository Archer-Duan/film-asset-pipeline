from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import utc_now


REVIEW_VALUES = {"pending", "approved", "rejected"}


class WorkflowStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_metadata (
                    asset_id TEXT PRIMARY KEY,
                    title TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    image_review TEXT NOT NULL DEFAULT 'pending',
                    model_review TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.commit()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.rollback()
            except sqlite3.OperationalError as exc:
                self._connection.close()
                raise RuntimeError(
                    f"审核数据库不可写：{db_path}。请使用有本地写入权限的账号启动工作台。"
                ) from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get(self, asset_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM asset_metadata WHERE asset_id=?", (asset_id,)
            ).fetchone()
        if row is None:
            return {
                "title": None,
                "tags": [],
                "notes": "",
                "image_review": "pending",
                "model_review": "pending",
                "updated_at": None,
            }
        result = dict(row)
        try:
            result["tags"] = json.loads(str(result.pop("tags_json")))
        except json.JSONDecodeError:
            result["tags"] = []
        return result

    def update(
        self,
        asset_id: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
        image_review: str | None = None,
        model_review: str | None = None,
    ) -> dict[str, Any]:
        current = self.get(asset_id)
        if image_review is not None and image_review not in REVIEW_VALUES:
            raise ValueError("image_review 值无效")
        if model_review is not None and model_review not in REVIEW_VALUES:
            raise ValueError("model_review 值无效")
        clean_tags = current["tags"] if tags is None else _clean_tags(tags)
        values = {
            "title": current["title"] if title is None else title.strip()[:120],
            "tags": clean_tags,
            "notes": current["notes"] if notes is None else notes.strip()[:2000],
            "image_review": image_review or current["image_review"],
            "model_review": model_review or current["model_review"],
            "updated_at": utc_now(),
        }
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO asset_metadata (
                    asset_id, title, tags_json, notes, image_review, model_review, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    title=excluded.title,
                    tags_json=excluded.tags_json,
                    notes=excluded.notes,
                    image_review=excluded.image_review,
                    model_review=excluded.model_review,
                    updated_at=excluded.updated_at
                """,
                (
                    asset_id,
                    values["title"],
                    json.dumps(values["tags"], ensure_ascii=False),
                    values["notes"],
                    values["image_review"],
                    values["model_review"],
                    values["updated_at"],
                ),
            )
            self._connection.commit()
        return self.get(asset_id)


def load_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def build_asset_inventory(
    image_manifest: Path,
    model_manifest: Path,
    metadata: WorkflowStore,
    input_dir: Path | None = None,
    extra_images: list[dict[str, Any]] | None = None,
    extra_models: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    # manifest 是生成流水线的事实来源，审核库只保存人工编辑的元数据；这里把两者拼成页面视图。
    image_rows = load_manifest(image_manifest) + (extra_images or [])
    model_rows = load_manifest(model_manifest) + (extra_models or [])
    image_rows = _preferred_image_rows(image_rows, model_rows)
    assets: list[dict[str, Any]] = []
    seen_asset_ids: set[str] = set()
    for row in image_rows:
        output_path = _existing_path(row.get("output_path"))
        if str(row.get("status")) != "complete" or output_path is None:
            continue
        output_hash = str(row.get("output_sha256") or row.get("source_sha256") or row.get("task_id"))
        asset_id = f"asset-{output_hash[:16]}"
        if asset_id in seen_asset_ids:
            continue
        seen_asset_ids.add(asset_id)
        matching_models = [
            item
            for item in model_rows
            if _same_path(item.get("source_path"), output_path)
        ]
        # 同一张 2D 图可能有多条 3D 记录，选择排序最高的一条作为当前展示状态。
        model = max(matching_models, key=_model_rank, default=None)
        saved = metadata.get(asset_id)
        inferred_image_review = (
            "approved"
            if saved["image_review"] == "pending" and matching_models
            else saved["image_review"]
        )
        task_rows = [
            item
            for item in image_rows
            if str(item.get("task_id")) == str(row.get("task_id"))
            and str(item.get("status")) == "complete"
            and _existing_path(item.get("output_path")) is not None
        ]
        result_index = int(row.get("result_index") or 1)
        result_count = len(task_rows)
        source_value = Path(str(row.get("source_path") or ""))
        source_name = _source_name(source_value, output_path)
        default_title = (
            f"{source_name} · 资产 {result_index:02d}"
            if result_count > 1
            else source_name
        )
        title = saved["title"] or default_title
        model_status = str(model.get("status")) if model else "not_started"
        stage = _stage(inferred_image_review, saved["model_review"], model_status)
        assets.append(
            {
                "asset_id": asset_id,
                "title": title,
                "tags": saved["tags"],
                "notes": saved["notes"],
                "stage": stage,
                "image_review": inferred_image_review,
                "model_review": saved["model_review"],
                "source_path": str(row.get("source_path") or ""),
                "image_path": str(output_path),
                "image_task_id": str(row.get("task_id") or ""),
                "image_model": str(row.get("model") or ""),
                "source_name": source_name,
                "target_name": _automatic_target_name(source_name),
                "source_file_name": Path(str(row.get("source_path") or "")).name,
                "result_index": result_index,
                "result_count": result_count,
                "logical_file_name": (
                    f"{source_name}__2D-{result_index:02d}__{asset_id}"
                    f"{output_path.suffix.lower()}"
                ),
                "model_task_id": str(model.get("task_id") or "") if model else None,
                "model_status": model_status,
                "model_path": str(model.get("output_path") or "") if model else None,
                "preview_path": str(model.get("preview_path") or "") if model else None,
                "face_count": int(model.get("face_count") or 0) if model else None,
                "enable_pbr": bool(model.get("enable_pbr")) if model else None,
                "credits_consumed": model.get("credits_consumed") if model else None,
                "generated_at": (
                    model.get("updated_at") if model and model.get("updated_at") else row.get("updated_at")
                ),
                "updated_at": saved["updated_at"] or row.get("updated_at"),
            }
        )
    if input_dir is not None:
        failed_by_hash: dict[str, dict[str, Any]] = {}
        failed_by_path: dict[Path, dict[str, Any]] = {}
        for row in image_rows:
            if str(row.get("status")) != "failed":
                continue
            source_hash = str(row.get("source_sha256") or "")
            if source_hash and str(row.get("updated_at") or "") >= str(
                failed_by_hash.get(source_hash, {}).get("updated_at") or ""
            ):
                failed_by_hash[source_hash] = row
            if row.get("source_path"):
                source_path = Path(str(row["source_path"])).resolve()
                if str(row.get("updated_at") or "") >= str(
                    failed_by_path.get(source_path, {}).get("updated_at") or ""
                ):
                    failed_by_path[source_path] = row
        processed_sources = {
            Path(str(row.get("source_path"))).resolve()
            for row in image_rows
            if str(row.get("status")) == "complete" and row.get("source_path")
        }
        processed_hashes = {
            str(row.get("source_sha256"))
            for row in image_rows
            if str(row.get("status")) == "complete" and row.get("source_sha256")
        }
        seen_hashes: set[str] = set()
        if input_dir.is_dir():
            for source_path in sorted(input_dir.rglob("*")):
                if (
                    not source_path.is_file()
                    or source_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
                ):
                    continue
                source_hash = _file_sha256(source_path)
                if source_hash in seen_hashes:
                    continue
                seen_hashes.add(source_hash)
                source_processed = (
                    source_path.resolve() in processed_sources or source_hash in processed_hashes
                )
                asset_id = f"frame-{source_hash[:16]}"
                saved = metadata.get(asset_id)
                source_name = source_path.stem.split("__", 1)[0]
                failed_row = failed_by_hash.get(source_hash) or failed_by_path.get(
                    source_path.resolve()
                )
                assets.append(
                    {
                        "asset_id": asset_id,
                        "title": saved["title"] or source_path.stem.split("__", 1)[0],
                        "tags": saved["tags"],
                        "notes": saved["notes"],
                        "stage": "source_frame" if source_processed else "awaiting_2d",
                        "source_status": "processed" if source_processed else "awaiting_2d",
                        "image_review": "pending",
                        "model_review": "pending",
                        "source_path": str(source_path.resolve()),
                        "source_name": source_name,
                        "source_file_name": source_path.name,
                        "target_name": _automatic_target_name(saved["title"] or source_name),
                        "image_path": str(source_path.resolve()),
                        "image_task_id": (
                            str(failed_row.get("task_id") or "") if failed_row else None
                        ),
                        "image_model": None,
                        "image_error_code": (
                            str(failed_row.get("error_code") or "") if failed_row else None
                        ),
                        "image_error_message": (
                            str(failed_row.get("error_message") or "") if failed_row else None
                        ),
                        "image_attempts": (
                            int(failed_row.get("attempts") or 0) if failed_row else 0
                        ),
                        "model_task_id": None,
                        "model_status": "not_started",
                        "model_path": None,
                        "preview_path": None,
                        "face_count": None,
                        "enable_pbr": None,
                        "credits_consumed": None,
                        "generated_at": datetime.fromtimestamp(
                            source_path.stat().st_mtime, tz=UTC
                        ).isoformat(),
                        "updated_at": saved["updated_at"] or datetime.fromtimestamp(
                            source_path.stat().st_mtime, tz=UTC
                        ).isoformat(),
                    }
                )
    return sorted(assets, key=lambda item: (item["stage"], item["title"], item["asset_id"]))


def inventory_summary(assets: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": 0,
        "source_frames": 0,
        "awaiting_2d": 0,
        "image_review": 0,
        "ready_for_3d": 0,
        "model_generation": 0,
        "model_review": 0,
        "approved": 0,
        "rejected": 0,
    }
    for asset in assets:
        stage = str(asset["stage"])
        if stage in {"source_frame", "awaiting_2d"}:
            summary["source_frames"] += 1
        if stage != "source_frame":
            summary["total"] += 1
        if stage in summary:
            summary[stage] += 1
    return summary


def _file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_name(source_path: Path, output_path: Path) -> str:
    if source_path.is_file():
        return source_path.stem.split("__", 1)[0]
    return output_path.stem.split("__", 1)[0]


def _automatic_target_name(value: str) -> str | None:
    candidate = str(value or "").strip()
    if "· 资产" in candidate:
        candidate = candidate.split("· 资产", 1)[0].strip()
    lowered = candidate.lower().replace("-", "").replace("_", "").replace(" ", "")
    generic_prefixes = ("frame", "image", "img", "screenshot", "screen", "截图", "屏幕截图", "微信图片")
    if not candidate or candidate.isdigit() or any(lowered.startswith(prefix) for prefix in generic_prefixes):
        return None
    return candidate[:80]


def _preferred_image_rows(
    image_rows: list[dict[str, Any]], model_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep one completed 2D attempt per source, plus outputs already linked to 3D."""
    tasks: dict[str, list[dict[str, Any]]] = {}
    for row in image_rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            tasks.setdefault(task_id, []).append(row)

    preferred_by_source: dict[str, tuple[str, str]] = {}
    for task_id, rows in tasks.items():
        completed = [
            row
            for row in rows
            if str(row.get("status")) == "complete"
            and _existing_path(row.get("output_path")) is not None
        ]
        if not completed:
            continue
        sample = completed[0]
        source_key = str(sample.get("source_sha256") or sample.get("source_path") or task_id)
        updated_at = max(str(row.get("updated_at") or "") for row in completed)
        if source_key not in preferred_by_source or updated_at > preferred_by_source[source_key][0]:
            preferred_by_source[source_key] = (updated_at, task_id)

    keep_task_ids = {task_id for _, task_id in preferred_by_source.values()}
    linked_2d_paths = {
        Path(str(row.get("source_path"))).resolve()
        for row in model_rows
        if row.get("source_path")
    }
    for task_id, rows in tasks.items():
        if any(
            row.get("output_path")
            and Path(str(row["output_path"])).resolve() in linked_2d_paths
            for row in rows
        ):
            keep_task_ids.add(task_id)

    return [
        row
        for row in image_rows
        if str(row.get("task_id") or "") in keep_task_ids
        or str(row.get("status")) != "complete"
    ]


def _clean_tags(tags: list[str]) -> list[str]:
    result: list[str] = []
    for value in tags:
        tag = str(value).strip()[:40]
        if tag and tag not in result:
            result.append(tag)
    return result[:30]


def _existing_path(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).resolve()
    return path if path.is_file() else None


def _same_path(value: Any, path: Path) -> bool:
    if not value:
        return False
    try:
        return Path(str(value)).resolve() == path.resolve()
    except OSError:
        return False


def _model_rank(row: dict[str, Any]) -> tuple[int, str]:
    rank = {
        "complete": 5,
        "running": 4,
        "submitted": 4,
        "pending": 3,
        "retry": 3,
        "failed": 1,
    }.get(str(row.get("status")), 0)
    return rank, str(row.get("updated_at") or "")


def _stage(image_review: str, model_review: str, model_status: str) -> str:
    if image_review == "rejected" or model_review == "rejected":
        return "rejected"
    if image_review != "approved":
        return "image_review"
    if model_status == "not_started" or model_status == "failed":
        return "ready_for_3d"
    if model_status != "complete":
        return "model_generation"
    return "approved" if model_review == "approved" else "model_review"
