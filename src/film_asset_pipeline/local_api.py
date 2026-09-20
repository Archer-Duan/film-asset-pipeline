from io import BytesIO
from pathlib import Path
import hashlib
import json
import os
import tempfile

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel


from PIL import Image

from .pipeline import sanitize_filename


class WorkflowSwitch(BaseModel):
    version: str
    enabled: bool


def install_local_routes(app, engine, assets_dir):
    @app.get("/objects")
    def objects_page():
        return FileResponse(assets_dir / "objects.html")

    @app.get("/login")
    def login_page():
        return FileResponse(assets_dir / "login.html")

    @app.get("/api/local/status")
    def status():
        return engine.health()

    @app.get("/api/local/tasks")
    def tasks():
        return {"tasks": engine.public_tasks()}

    @app.post("/api/local/tasks", status_code=202)
    async def submit(
        request: Request,
        files: list[UploadFile] = File(...),
        kind: str = Form("edit"),
        prompt: str = Form(""),
        title: str = Form(""),
        parameters: str = Form("{}"),
        input_keys: str = Form("[]"),
        version: str | None = Form(None),
    ):
        if not engine.config.get("enabled", False):
            raise HTTPException(409, "请在算力主机配置 comfy.local.json 并启用本地服务")
        if len(files) > 50 or len(prompt) > 8000:
            raise HTTPException(400, "每批最多 50 张图，提示词最多 8000 字符")
        sources = []
        try:
            if len(parameters) > 32000 or len(input_keys) > 4096:
                raise ValueError("输入参数过长")
            params, keys = json.loads(parameters), json.loads(input_keys)
            profile, _ = engine.profile(kind, version)
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                raise ValueError("图片输入名称格式错误")
            if len(profile["inputs"]) > 1 or keys:
                if (
                    len(keys) != len(files)
                    or len(set(keys)) != len(keys)
                    or set(keys) != {f["key"] for f in profile["inputs"]}
                ):
                    raise ValueError("请分别提供工作流要求的全部图片，视图名称不能重复")
            for upload in files:
                data = await upload.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("单张图片不能超过 20MB")
                with Image.open(BytesIO(data)) as im:
                    if (
                        im.format not in ("JPEG", "PNG", "WEBP")
                        or im.width * im.height > 40_000_000
                    ):
                        raise ValueError("需要 JPG、PNG 或 WebP，最多 4000 万像素")
                    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[im.format]
                    im.verify()
                folder = engine.folder / "uploads"
                folder.mkdir(exist_ok=True)
                stem = sanitize_filename(Path(upload.filename or "reference").stem)[:80]
                path = folder / (
                    stem + "__" + hashlib.sha256(data).hexdigest() + suffix
                )
                if not path.exists():
                    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temp:
                        temp.write(data)
                        temp_path = Path(temp.name)
                    try:
                        os.replace(temp_path, path)
                    finally:
                        temp_path.unlink(missing_ok=True)
                sources.append(path)
            return engine.submit(
                sources,
                kind,
                prompt,
                request.state.member,
                input_sets=[dict(zip(keys, sources))] if keys else None,
                parameters=params,
                version=version,
                title=title,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/local/workflows")
    def workflows():
        return {"workflows": engine.library.entries()}

    @app.get("/api/local/workflows/{kind}/export")
    def export(kind: str, version: str, request: Request):
        if request.state.member != "local":
            raise HTTPException(403, "仅主机管理员可导出工作流")
        try:
            profile, workflow = engine.profile(kind, version)
            for key in ("file", "enabled", "builtin", "image", "prompt"):
                profile.pop(key, None)
            return JSONResponse(
                {"manifest": profile, "workflow": workflow},
                headers={
                    "Content-Disposition": f'attachment; filename="{profile["id"]}-{profile["version"]}.json"'
                },
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/local/workflows/import", status_code=201)
    async def import_workflow(request: Request, file: UploadFile = File(...)):
        if request.state.member != "local":
            raise HTTPException(403, "仅主机管理员可导入工作流")
        data = await file.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(400, "工作流包最多 5MB")
        try:
            manifest = engine.library.import_bundle(json.loads(data))
            return {
                "id": manifest["id"],
                "version": manifest["version"],
                "enabled": False,
            }
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/local/workflows/{kind}/enabled")
    def enable(kind: str, body: WorkflowSwitch, request: Request):
        if request.state.member != "local":
            raise HTTPException(403, "仅主机管理员可启停工作流")
        try:
            engine.enable_workflow(kind, body.version, body.enabled)
            return {"enabled": body.enabled}
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def task_record(task_id):
        row = next((r for r in engine.rows() if r["id"] == task_id), None)
        if not row:
            raise HTTPException(404, "任务不存在")
        return row

    @app.get("/api/local/tasks/{task_id}/inputs/{key}")
    def input_file(task_id: str, key: str):
        row = task_record(task_id)
        path = json.loads(row["input_files"]).get(key)
        if not path or not Path(path).is_file():
            raise HTTPException(404, "输入图片不存在")
        return FileResponse(path)

    @app.get("/api/local/tasks/{task_id}/artifacts/{artifact_id}")
    def artifact_file(task_id: str, artifact_id: str):
        row = task_record(task_id)
        item = json.loads(row["artifacts"]).get(artifact_id)
        if not item or not Path(item["path"]).is_file():
            raise HTTPException(404, "生成结果不存在")
        return FileResponse(
            item["path"],
            filename=item["name"],
            content_disposition_type="attachment"
            if item["role"] == "model"
            else "inline",
        )

    @app.post("/api/local/tasks/{task_id}/retry")
    def retry(task_id: str, request: Request):
        try:
            return engine.retry(task_id, request.state.member)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/local/tasks/{task_id}/files/{role}")
    def file(task_id: str, role: str):
        if role not in ("source", "image", "model"):
            raise HTTPException(404, "文件类型不存在")
        row = next((r for r in engine.rows() if r["id"] == task_id), None)
        if not row or not row.get(role):
            raise HTTPException(404, "文件尚未生成")
        path = Path(row[role])
        if not path.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(
            path,
            filename=path.name,
            content_disposition_type="attachment" if role == "model" else "inline",
        )
