from io import BytesIO
from pathlib import Path
import hashlib
import os
import tempfile

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image

from .pipeline import sanitize_filename


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
    ):
        if not engine.config.get("enabled", False):
            raise HTTPException(409, "请在算力主机配置 comfy.local.json 并启用本地服务")
        if len(files) > 50 or len(prompt) > 8000:
            raise HTTPException(400, "每批最多 50 张图，提示词最多 8000 字符")
        if kind not in ("edit", "model", "full"):
            raise HTTPException(400, "该工作流暂未开放")
        sources = []
        try:
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
            return engine.submit(sources, kind, prompt, request.state.member)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

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
