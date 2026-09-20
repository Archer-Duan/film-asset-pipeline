from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
import json
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from .asset_files import download_name
from pydantic import BaseModel, Field

from .config import load_settings
from .credentials import (
    SECRET_NAMES,
    delete_secret,
    get_secret,
    masked,
    save_preferences,
    set_secret,
)
from .hunyuan_config import load_hunyuan_settings
from .pipeline import SUPPORTED_SUFFIXES, sanitize_filename, sha256_file
from .runtime import default_config_path
from .local_engine import LocalEngine, local_config
from .local_api import install_local_routes
from .team_auth import TeamAuth
from .state import utc_now
from .workflow import (
    WorkflowStore,
    build_asset_inventory,
    inventory_summary,
    load_manifest,
)


LOGGER = logging.getLogger(__name__)
FRONTEND_VERSION = "1.2.0"


class AssetUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ReviewUpdate(BaseModel):
    stage: Literal["image", "model"]
    decision: Literal["pending", "approved", "rejected"]


class BatchReviewRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    stage: Literal["image", "model", "auto"]
    decision: Literal["pending", "approved", "rejected"]


class DownloadRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    kind: Literal["model", "image", "source"] = "model"


class GenerateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    profile: Literal["minimal", "production"] = "minimal"
    confirmation: Literal["GENERATE_3D"]


class ProcessImagesRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    output_count: Literal[1, 3] = 3
    confirmation: Literal["PROCESS_2D"]
    prompt: str = Field(default="", max_length=8000)


class CredentialsUpdate(BaseModel):
    ark_api_key: str | None = Field(default=None, max_length=1024, repr=False)
    hunyuan_api_style: Literal["tencentcloud_sdk", "ai3d_openai"] = "tencentcloud_sdk"
    hunyuan_api_key: str | None = Field(default=None, max_length=1024, repr=False)
    tencentcloud_secret_id: str | None = Field(
        default=None, max_length=1024, repr=False
    )
    tencentcloud_secret_key: str | None = Field(
        default=None, max_length=1024, repr=False
    )


class CredentialDeleteRequest(BaseModel):
    names: list[
        Literal[
            "ARK_API_KEY",
            "HUNYUAN_3D_API_KEY",
            "TENCENTCLOUD_SECRET_ID",
            "TENCENTCLOUD_SECRET_KEY",
        ]
    ] = Field(min_length=1, max_length=4)


class JobManager:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="workflow-job"
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def start(self, kind: str, fn: Any) -> dict[str, Any]:
        # Web 请求只负责入队并立即返回；实际生成在后台串行执行，避免阻塞 HTTP 请求。
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "message": "任务已进入本地串行队列",
            "progress": {
                "total": 0,
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "percent": 0,
                "current_item": None,
            },
        }
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run, job_id, fn)
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return dict(job)

    def _run(self, job_id: str, fn: Any) -> None:
        self._set(job_id, status="running", message="任务执行中")

        def update(message: str, **progress: Any) -> None:
            self._set(job_id, message=message, progress=progress)

        try:
            message = str(fn(update))
        except Exception as exc:
            self._set(job_id, status="failed", message=str(exc)[:2000])
        else:
            self._set(job_id, status="complete", message=message[:2000])

    def _set(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)
            self._jobs[job_id]["updated_at"] = utc_now()


def create_app(config_path: Path | str = Path("config.toml")) -> FastAPI:
    # 应用启动时只组装配置、数据库和服务对象；具体资产列表在请求时从 manifest 重新计算。
    config_path = Path(config_path).resolve()
    settings = load_settings(config_path)
    model_settings = load_hunyuan_settings(config_path)
    metadata = WorkflowStore(settings.root_dir / "data/state/workflow.sqlite3")
    jobs = JobManager()
    upload_lock = threading.RLock()
    assets_dir = Path(__file__).with_name("web_assets")
    local_engine = LocalEngine(settings.root_dir)
    local_enabled = local_engine.config.get("enabled", False)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if local_enabled:
            local_engine.start()
        yield
        local_engine.close()
        jobs.close()
        metadata.close()

    app = FastAPI(title="Film Asset Workflow", version="1.2.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=assets_dir), name="static")
    TeamAuth(settings.root_dir).install(app)
    install_local_routes(app, local_engine, assets_dir)
    app.state.local_engine = local_engine

    @app.middleware("http")
    async def prevent_stale_frontend(request: Any, call_next: Any):
        response = await call_next(request)
        if (
            request.url.path == "/"
            or request.url.path == "/api/workspace"
            or request.url.path.startswith("/static/")
        ):
            response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(_: Any, exc: Exception) -> JSONResponse:
        error_id = uuid.uuid4().hex[:8]
        LOGGER.exception("Unhandled web error %s", error_id, exc_info=exc)
        if isinstance(exc, sqlite3.OperationalError) and "readonly" in str(exc).lower():
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "审核数据库当前不可写。请重启本地工作台服务后重试；审核结果尚未保存。",
                    "error_id": error_id,
                },
            )
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"服务器处理失败（错误编号 {error_id}），请重试；若持续出现请查看本地服务日志。",
                "error_id": error_id,
            },
        )

    def inventory() -> list[dict[str, Any]]:
        local_images, local_models = local_engine.manifest_rows()
        return build_asset_inventory(
            settings.output_dir / "manifest.json",
            model_settings.output_dir / "manifest-3d.json",
            metadata,
            settings.input_dir,
            local_images,
            local_models,
            local_engine.source_rows(),
        )

    app.state.asset_inventory = inventory

    def find_asset(asset_id: str) -> dict[str, Any]:
        asset = next(
            (item for item in inventory() if item["asset_id"] == asset_id), None
        )
        if asset is None:
            raise HTTPException(status_code=404, detail="资产不存在")
        return asset

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(assets_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "frontend_version": FRONTEND_VERSION}

    @app.get("/api/workspace")
    def workspace() -> dict[str, Any]:
        assets = inventory()
        return {
            "frontend_version": FRONTEND_VERSION,
            "summary": inventory_summary(assets),
            "assets": [_public_asset(item) for item in assets],
            "profiles": {
                "minimal": {"label": "测试 · 默认50万面", "estimated_credits": 20},
                "production": {
                    "label": "正式 · PBR + 100万面",
                    "estimated_credits": 45,
                },
            },
            "configuration": _configuration_status(config_path),
        }

    @app.get("/api/settings/status")
    def settings_status(request: Request) -> dict[str, Any]:
        _require_local_request(request)
        return _configuration_status(config_path)

    @app.post("/api/settings/credentials")
    def update_credentials(body: CredentialsUpdate, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        values = {
            "ARK_API_KEY": body.ark_api_key,
            "HUNYUAN_3D_API_KEY": body.hunyuan_api_key,
            "TENCENTCLOUD_SECRET_ID": body.tencentcloud_secret_id,
            "TENCENTCLOUD_SECRET_KEY": body.tencentcloud_secret_key,
        }
        storage_backends: set[str] = set()
        for name, value in values.items():
            if value is not None and value.strip():
                storage_backends.add(set_secret(name, value, settings.root_dir))
        save_preferences(
            settings.root_dir,
            {"hunyuan_api_style": body.hunyuan_api_style},
        )
        status = _configuration_status(config_path)
        status["saved"] = True
        status["storage_backend"] = (
            "system_keyring"
            if storage_backends == {"system_keyring"}
            else "local_env_file"
        )
        return status

    @app.delete("/api/settings/credentials")
    def remove_credentials(
        body: CredentialDeleteRequest, request: Request
    ) -> dict[str, Any]:
        _require_local_request(request)
        for name in body.names:
            delete_secret(name, settings.root_dir)
        return _configuration_status(config_path)

    @app.post("/api/settings/test")
    def test_credentials(request: Request) -> dict[str, Any]:
        _require_local_request(request)
        image_settings = load_settings(config_path)
        three_d_settings = load_hunyuan_settings(config_path)
        errors: list[str] = []
        try:
            image_settings.validate(require_key=True)
        except ValueError as exc:
            errors.append(f"即梦：{exc}")
        try:
            three_d_settings.validate(require_key=True)
        except ValueError as exc:
            errors.append(f"混元3D：{exc}")
        return {
            "valid": not errors,
            "errors": errors,
            "message": (
                "本地配置检查通过。为避免产生费用，本步骤不会向模型服务提交生成任务；首次真实任务将验证账号权限和额度。"
                if not errors
                else "配置尚未完整，请补充缺失项目。"
            ),
        }

    @app.get("/api/assets/{asset_id}/files/{kind}")
    def asset_file(asset_id: str, kind: Literal["source", "image", "preview", "model"]):
        asset = find_asset(asset_id)
        field = {
            "source": "source_path",
            "image": "image_path",
            "preview": "preview_path",
            "model": "model_path",
        }[kind]
        value = asset.get(field)
        if not value:
            raise HTTPException(status_code=404, detail="文件尚未生成")
        path = Path(str(value)).resolve()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        disposition = "attachment" if kind == "model" else "inline"
        return FileResponse(
            path,
            filename=download_name(asset, kind, path.suffix),
            content_disposition_type=disposition,
        )

    @app.post("/api/actions/download")
    def batch_download(body: DownloadRequest):
        assets = {a["asset_id"]: a for a in inventory()}
        selected = []
        for aid in dict.fromkeys(body.asset_ids):
            if aid not in assets:
                raise HTTPException(404, "所选资产不存在，请刷新后重试")
            asset = assets[aid]
            value = asset.get(
                {"model": "model_path", "image": "image_path", "source": "source_path"}[
                    body.kind
                ]
            )
            if not value or not Path(value).is_file():
                raise HTTPException(
                    409, f"{asset['title']} 的下载文件不存在；本次未打包，请重新选择"
                )
            if body.kind == "model" and asset["model_status"] != "complete":
                raise HTTPException(409, "所选模型尚未生成完成")
            selected.append((asset, Path(value)))
        if sum(p.stat().st_size for _, p in selected) > 5 * 1024**3:
            raise HTTPException(413, "单次下载最多 5GB，请分批选择")
        folder = local_engine.folder / "downloads"
        folder.mkdir(exist_ok=True)
        handle = tempfile.NamedTemporaryFile(suffix=".zip", dir=folder, delete=False)
        path = Path(handle.name)
        handle.close()
        try:
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                manifest = []
                for asset, file in selected:
                    name = download_name(asset, body.kind, file.suffix)
                    archive.write(file, arcname=name)
                    manifest.append(
                        {
                            "asset_id": asset["asset_id"],
                            "title": asset["title"],
                            "file": name,
                            "tags": asset["tags"],
                            "review": asset["model_review"]
                            if body.kind == "model"
                            else asset["image_review"],
                        }
                    )
                archive.writestr(
                    "资产清单.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            return FileResponse(
                path,
                media_type="application/zip",
                filename=f"资产批量下载_{body.kind}_{len(selected)}项.zip",
                background=BackgroundTask(path.unlink, missing_ok=True),
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @app.patch("/api/assets/{asset_id}")
    def update_asset(asset_id: str, body: AssetUpdate) -> dict[str, Any]:
        find_asset(asset_id)
        metadata.update(asset_id, title=body.title, tags=body.tags, notes=body.notes)
        return _public_asset(find_asset(asset_id))

    @app.post("/api/assets/{asset_id}/review")
    def review_asset(asset_id: str, body: ReviewUpdate) -> dict[str, Any]:
        find_asset(asset_id)
        values = {f"{body.stage}_review": body.decision}
        metadata.update(asset_id, **values)
        return _public_asset(find_asset(asset_id))

    @app.post("/api/actions/review")
    def batch_review(body: BatchReviewRequest) -> dict[str, Any]:
        selected = [find_asset(asset_id) for asset_id in dict.fromkeys(body.asset_ids)]
        if body.stage == "auto":
            if body.decision != "pending":
                raise HTTPException(
                    status_code=400, detail="重新提交审核时，审核状态必须为待审核"
                )
            invalid = [
                asset["title"] for asset in selected if asset["stage"] != "rejected"
            ]
        else:
            expected_stage = "image_review" if body.stage == "image" else "model_review"
            invalid = [
                asset["title"] for asset in selected if asset["stage"] != expected_stage
            ]
        if invalid:
            raise HTTPException(
                status_code=409,
                detail=f"所选资产不属于当前审核环节：{', '.join(invalid[:5])}",
            )

        updated: list[dict[str, Any]] = []
        for asset in selected:
            review_stage = body.stage
            if review_stage == "auto":
                review_stage = (
                    "model" if asset["model_review"] == "rejected" else "image"
                )
            metadata.update(
                asset["asset_id"], **{f"{review_stage}_review": body.decision}
            )
            updated.append(_public_asset(find_asset(asset["asset_id"])))
        return {"updated_count": len(updated), "assets": updated}

    @app.post("/api/frames/upload")
    async def upload_frames(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        try:
            settings.input_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.error(
                "Unable to prepare input directory %s: %s", settings.input_dir, exc
            )
            raise HTTPException(
                status_code=503, detail="输入文件夹当前不可写，请检查目录权限"
            ) from exc
        saved: list[str] = []
        duplicates: list[str] = []
        failed: list[dict[str, str]] = []
        prepared: list[tuple[str, str, bytes, str]] = []
        for upload in files[:100]:
            original_name = upload.filename or "未命名图片"
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                failed.append(
                    {
                        "name": original_name,
                        "reason": f"不支持的图片格式：{suffix or '未知'}",
                    }
                )
                continue
            data = await upload.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                failed.append({"name": original_name, "reason": "单张图片不能超过20MB"})
                continue
            content_hash = hashlib.sha256(data).hexdigest()
            stem = sanitize_filename(Path(upload.filename or "frame").stem)
            prepared.append(
                (
                    original_name,
                    f"{stem}__{content_hash[:8]}{suffix}",
                    data,
                    content_hash,
                )
            )

        # One lock covers duplicate detection and writes so simultaneous browser requests
        # cannot race on the same Windows filename.
        with upload_lock:
            existing_hashes: set[str] = set()
            for path in settings.input_dir.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                try:
                    existing_hashes.add(sha256_file(path))
                except OSError as exc:
                    LOGGER.warning(
                        "Unable to inspect existing upload %s: %s", path, exc
                    )
            for original_name, target_name, data, content_hash in prepared:
                if content_hash in existing_hashes:
                    duplicates.append(original_name)
                    continue
                target = settings.input_dir / target_name
                try:
                    target.write_bytes(data)
                except OSError as exc:
                    LOGGER.warning("Unable to save upload %s: %s", target, exc)
                    failed.append(
                        {
                            "name": original_name,
                            "reason": "无法写入输入文件夹，请稍后重试",
                        }
                    )
                    continue
                saved.append(target.name)
                existing_hashes.add(content_hash)
        return {
            "saved": saved,
            "count": len(saved),
            "duplicates": duplicates,
            "skipped_count": len(duplicates),
            "failed": failed,
            "failed_count": len(failed),
            "requested_count": min(len(files), 100),
        }

    @app.post("/api/actions/process-2d", status_code=202)
    def process_2d(body: ProcessImagesRequest, request: Request) -> dict[str, Any]:
        if local_enabled:
            if body.output_count != 1:
                raise HTTPException(
                    400, "本地 Qwen 当前生成单张补全图，多视图生成工作流尚未接入"
                )
            selected = [find_asset(aid) for aid in dict.fromkeys(body.asset_ids)]
            if any(
                a.get("model_status") == "complete"
                or a["stage"] in ("image_generation", "model_generation")
                for a in selected
            ):
                raise HTTPException(
                    409, "任务正在生成或已有模型，请使用原始素材创建新的补图任务"
                )
            sources = list(dict.fromkeys(a["source_path"] for a in selected))
            try:
                return local_engine.submit(
                    sources,
                    "edit",
                    body.prompt,
                    request.state.member,
                    titles={a["source_path"]: a["title"][:80] for a in selected},
                )
            except (ValueError, OSError) as exc:
                raise HTTPException(409, str(exc)) from exc
        current_settings = load_settings(config_path)
        try:
            current_settings.validate(require_key=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        requested = [find_asset(asset_id) for asset_id in dict.fromkeys(body.asset_ids)]
        invalid = [
            asset["title"]
            for asset in requested
            if asset["stage"]
            not in {"source_frame", "awaiting_2d", "image_review", "rejected"}
            or asset.get("model_status") == "complete"
        ]
        if invalid:
            raise HTTPException(
                status_code=409, detail="只能优化尚未进入3D制作的静帧或2D资产"
            )
        selected_by_source: dict[str, dict[str, Any]] = {}
        for asset in requested:
            selected_by_source.setdefault(str(asset["source_path"]), asset)
        selected = list(selected_by_source.values())

        def run_selected(update: Any) -> str:
            total = len(selected)
            succeeded = 0
            failed = 0
            failures: list[str] = []
            for index, asset in enumerate(selected, start=1):
                title = str(asset["title"])
                target_name = str(
                    asset.get("target_name") or asset.get("source_name") or title
                )
                update(
                    f"正在处理第 {index}/{total} 张：{title}",
                    total=total,
                    completed=index - 1,
                    succeeded=succeeded,
                    failed=failed,
                    percent=round((index - 1) / total * 100),
                    current_item=title,
                )
                try:
                    _run_cli(
                        config_path,
                        [
                            "run",
                            "--input",
                            str(asset["source_path"]),
                            "--mode",
                            "single" if body.output_count == 1 else "group",
                            "--max-images",
                            str(body.output_count),
                            "--concurrency",
                            "1",
                            "--prompt",
                            _targeted_prompt(
                                current_settings.prompt,
                                target_name,
                                uuid.uuid4().hex[:8],
                                body.output_count,
                            ),
                        ],
                    )
                except RuntimeError as exc:
                    failed += 1
                    failures.append(
                        f"{title}：{_describe_2d_failure(settings.output_dir / 'manifest.json', Path(str(asset['source_path'])), str(exc))}"
                    )
                else:
                    succeeded += 1
                update(
                    f"已完成 {index}/{total} 张；成功 {succeeded}，失败 {failed}",
                    total=total,
                    completed=index,
                    succeeded=succeeded,
                    failed=failed,
                    percent=round(index / total * 100),
                    current_item=title,
                )
            if failed:
                raise RuntimeError(
                    f"2D优化完成：成功 {succeeded} 张，失败 {failed} 张。\n"
                    + "\n".join(failures)
                )
            view_description = (
                "一张完整主视图" if body.output_count == 1 else "同一物品的三个不同视图"
            )
            return (
                f"2D优化完成：成功 {succeeded}/{total} 个来源静帧。"
                f"每个来源生成{view_description}，请进入“2D待审核”查看结果。"
            )

        return jobs.start("process-2d", run_selected)

    @app.post("/api/actions/generate-3d", status_code=202)
    def generate_3d(body: GenerateRequest, request: Request) -> dict[str, Any]:
        if local_enabled:
            selected = [find_asset(aid) for aid in dict.fromkeys(body.asset_ids)]
            if any(a["stage"] != "ready_for_3d" for a in selected):
                raise HTTPException(409, "请先通过图片审核")
            try:
                return local_engine.submit(
                    [a["image_path"] for a in selected],
                    "model",
                    owner=request.state.member,
                    originals=[a["source_path"] for a in selected],
                    titles={a["image_path"]: a["title"][:80] for a in selected},
                )
            except (ValueError, OSError) as exc:
                raise HTTPException(409, str(exc)) from exc
        current_model_settings = load_hunyuan_settings(config_path)
        try:
            current_model_settings.validate(require_key=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        selected = [find_asset(asset_id) for asset_id in dict.fromkeys(body.asset_ids)]

        def run_selected(update: Any) -> str:
            messages: list[str] = []
            failures: list[str] = []
            succeeded = 0
            failed = 0
            total = len(selected)
            for index, asset in enumerate(selected, start=1):
                update(
                    f"正在生成第 {index}/{total} 项3D：{asset['title']}",
                    total=total,
                    completed=index - 1,
                    succeeded=succeeded,
                    failed=failed,
                    percent=round((index - 1) / total * 100),
                    current_item=asset["title"],
                )
                source_path = Path(str(asset["image_path"]))
                manifest_path = model_settings.output_dir / "manifest-3d.json"
                command_error = ""
                try:
                    failed_task_id = _matching_failed_task(
                        manifest_path,
                        source_path,
                        body.profile,
                    )
                    if failed_task_id:
                        _run_cli(config_path, ["retry-3d", "--task-id", failed_task_id])
                    arguments = ["run-3d", "--input", str(source_path)]
                    if body.profile == "minimal":
                        arguments.append("--minimal-request")
                    _run_cli(config_path, arguments)
                except Exception as exc:
                    # run-3d 的退出码包含全库历史失败统计，不能直接当成本项结果。
                    command_error = str(exc)

                row = _matching_model_task(manifest_path, source_path, body.profile)
                status = str((row or {}).get("status") or "")
                if status == "complete":
                    succeeded += 1
                    messages.append(f"{asset['title']}：生成完成")
                elif status in {
                    "pending",
                    "retry",
                    "submitting",
                    "submitted",
                    "running",
                }:
                    succeeded += 1
                    messages.append(f"{asset['title']}：任务已提交，正在服务端处理")
                else:
                    failed += 1
                    detail = _describe_3d_failure(row, command_error)
                    failures.append(f"{asset['title']}：{detail}")
                update(
                    f"已处理 {index}/{total} 项3D",
                    total=total,
                    completed=index,
                    succeeded=succeeded,
                    failed=failed,
                    percent=round(index / total * 100),
                    current_item=asset["title"],
                )
            summary = f"3D批量处理完成：成功或已提交 {succeeded} 项，失败 {failed} 项。"
            if failures:
                raise RuntimeError(summary + "\n" + "\n".join(failures))
            return summary + ("\n" + "\n".join(messages) if messages else "")

        return jobs.start(f"generate-3d:{body.profile}", run_selected)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            if len(job_id) == 32:
                return local_engine.group(job_id)
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    return app


def _require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="密钥设置只允许从本机访问")


def _configuration_status(config_path: Path) -> dict[str, Any]:
    image_settings = load_settings(config_path)
    model_settings = load_hunyuan_settings(config_path)
    root = config_path.parent
    values = {name: get_secret(name, root) for name in SECRET_NAMES}
    if model_settings.api_style == "tencentcloud_sdk":
        hunyuan_configured = bool(
            values["TENCENTCLOUD_SECRET_ID"] and values["TENCENTCLOUD_SECRET_KEY"]
        )
    else:
        hunyuan_configured = bool(values["HUNYUAN_3D_API_KEY"])
    return {
        "setup_required": not bool(image_settings.api_key)
        and not local_config(root).get("enabled", False),
        "local_enabled": local_config(root).get("enabled", False),
        "ark": {
            "configured": bool(image_settings.api_key),
            "masked_key": masked(image_settings.api_key),
            "model": image_settings.model,
        },
        "hunyuan": {
            "configured": hunyuan_configured,
            "api_style": model_settings.api_style,
            "masked_api_key": masked(values["HUNYUAN_3D_API_KEY"]),
            "masked_secret_id": masked(values["TENCENTCLOUD_SECRET_ID"]),
            "masked_secret_key": masked(values["TENCENTCLOUD_SECRET_KEY"]),
            "model": model_settings.model,
        },
        "privacy": {
            "local_only": True,
            "secrets_returned": False,
            "generation_test_is_billable": False,
        },
    }


def _public_asset(asset: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in asset.items()
        if key
        not in {
            "source_path",
            "image_path",
            "preview_path",
            "model_path",
            "source_paths",
        }
    }
    asset_id = str(asset["asset_id"])
    result.update(
        {
            "source_url": f"/api/assets/{asset_id}/files/source",
            "image_url": f"/api/assets/{asset_id}/files/image",
            "preview_url": (
                f"/api/assets/{asset_id}/files/preview"
                if asset.get("preview_path")
                else None
            ),
            "model_url": (
                f"/api/assets/{asset_id}/files/model"
                if asset.get("model_path")
                else None
            ),
        }
    )
    return result


def _run_cli(config_path: Path, arguments: list[str]) -> str:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "film_asset_pipeline.cli",
            "--config",
            str(config_path),
            *arguments,
        ],
        cwd=config_path.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
        check=False,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode:
        raise RuntimeError(output or f"命令执行失败，退出码 {result.returncode}")
    return output or "完成"


def _describe_2d_failure(manifest_path: Path, source_path: Path, fallback: str) -> str:
    matching: list[dict[str, Any]] = []
    for row in load_manifest(manifest_path):
        try:
            same_source = (
                Path(str(row.get("source_path") or "")).resolve()
                == source_path.resolve()
            )
        except OSError:
            same_source = False
        if same_source and str(row.get("status")) == "failed":
            matching.append(row)
    if not matching:
        return fallback[:600]
    row = max(matching, key=lambda item: str(item.get("updated_at") or ""))
    code = str(row.get("error_code") or "接口错误")
    request_id = str(row.get("request_id") or "")
    if code == "SetLimitExceeded":
        detail = (
            "火山引擎 Seedream 5.0 的账号推理限额已触发，模型服务已暂停；"
            "这不是资源包余量不足。请在模型开通页调整或关闭“安心体验模式”后，再次勾选该静帧重试。"
        )
    else:
        detail = str(row.get("error_message") or fallback)[:500]
    return f"{code}：{detail}" + (f" 请求ID：{request_id}" if request_id else "")


def _targeted_prompt(
    base_prompt: str,
    target_name: str,
    batch_token: str,
    output_count: Literal[1, 3] = 3,
) -> str:
    target = " ".join(str(target_name or "").split())[:80]
    if not target:
        return base_prompt
    view_instruction = (
        "只生成一张目标资产的完整主视图，不得生成组图或多个方案。"
        if output_count == 1
        else "三张输出必须是同一件目标资产，仅视角不同。"
    )
    return (
        f"系统根据上传文件名或资产名称自动得到目标字段：“{target}”。"
        f"本次唯一目标资产是“{target}”。只定位、提取并补全输入图中的“{target}”本体，"
        "即使它在画面中占比较小，也不得改选展示盒、容器、人物、服装、武器或其他物品；"
        f"{view_instruction}\n" + base_prompt + f"\n[workflow_batch:{batch_token}]"
    )


def _matching_failed_task(
    manifest_path: Path, source_path: Path, profile: str
) -> str | None:
    row = _matching_model_task(manifest_path, source_path, profile, status="failed")
    return str((row or {}).get("task_id") or "") or None


def _matching_model_task(
    manifest_path: Path,
    source_path: Path,
    profile: str,
    *,
    status: str | None = None,
) -> dict[str, Any] | None:
    expected_pbr = profile == "production"
    expected_faces = 1_000_000 if expected_pbr else 500_000
    matching: list[dict[str, Any]] = []
    for row in load_manifest(manifest_path):
        try:
            same_source = (
                Path(str(row.get("source_path") or "")).resolve()
                == source_path.resolve()
            )
        except OSError:
            same_source = False
        if (
            same_source
            and (status is None or str(row.get("status")) == status)
            and bool(row.get("enable_pbr")) == expected_pbr
            and int(row.get("face_count") or 0) == expected_faces
        ):
            matching.append(row)
    if not matching:
        return None
    return max(matching, key=lambda item: str(item.get("updated_at") or ""))


def _describe_3d_failure(row: dict[str, Any] | None, fallback: str) -> str:
    if not row:
        concise = (
            fallback.strip()[:600]
            if fallback.strip()
            else "未找到本项3D任务记录，请查看本地服务日志。"
        )
        return concise
    code = str(row.get("error_code") or "3D接口错误")
    request_id = str(row.get("request_id") or "")
    known = {
        "ResourceInsufficient": (
            "腾讯混元3D服务返回“资源不足”。请检查当前密钥所属账号的余额、资源包、"
            "调用资格及模型服务额度；确认资源可用后，可直接重新勾选该资产生成。"
        ),
        "AccountOverdueError": "当前账号存在欠费，请充值或结清后重试。",
        "AuthFailure": "腾讯云身份认证失败，请检查 SecretId、SecretKey、账号权限与地域配置。",
        "UnauthorizedOperation": "当前密钥没有调用混元3D服务的权限，请检查账号授权。",
        "RequestLimitExceeded": "接口请求频率已超限，请稍后重试或降低批量并发。",
    }
    detail = (
        known.get(code)
        or str(row.get("error_message") or fallback or "接口调用失败")[:600]
    )
    return f"{code}：{detail}" + (f" 请求ID：{request_id}" if request_id else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动电影数字资产工作流界面")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.config), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
