from __future__ import annotations

import csv
import hashlib
import json
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from .hunyuan import HunyuanApiError, Queried3DJob, Remote3DFile, Submitted3DJob
from .hunyuan_config import HunyuanSettings
from .model_state import ModelStateStore, ModelTask
from .pipeline import SUPPORTED_SUFFIXES, atomic_write, sanitize_filename, sha256_file


class ModelClient(Protocol):
    def submit(self, source_path: Path) -> Submitted3DJob: ...

    def query(self, job_id: str) -> Queried3DJob: ...

    def download(self, url: str) -> bytes: ...


class Mock3DClient:
    def submit(self, source_path: Path) -> Submitted3DJob:
        job_id = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:16]
        return Submitted3DJob(job_id=job_id, request_id="mock-submit", status="queued")

    def query(self, job_id: str) -> Queried3DJob:
        return Queried3DJob(
            job_id=job_id,
            request_id="mock-query",
            status="completed",
            files=[Remote3DFile(file_type="glb", url=f"mock://{job_id}.glb", preview_url=None)],
            credits_consumed=0,
        )

    def download(self, url: str) -> bytes:
        json_chunk = b'{"asset":{"version":"2.0","generator":"film-assets-mock"}}'
        json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
        total_length = 12 + 8 + len(json_chunk)
        return (
            struct.pack("<4sII", b"glTF", 2, total_length)
            + struct.pack("<I4s", len(json_chunk), b"JSON")
            + json_chunk
        )


class ModelBatchPipeline:
    def __init__(
        self,
        settings: HunyuanSettings,
        store: ModelStateStore,
        client: ModelClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client

    def scan(self) -> tuple[int, int]:
        # 3D 输入通常来自 2D manifest；只接收已完成的 2D 输出，避免把失败结果送入建模。
        sources = self._input_sources()
        added = 0
        for path in sources:
            source_hash = sha256_file(path)
            identity = "\0".join(
                [
                    str(path.resolve()),
                    source_hash,
                    self.settings.model,
                    str(self.settings.enable_pbr),
                    str(self.settings.face_count),
                    self.settings.generate_type,
                ]
            )
            # 3D 参数也是任务身份的一部分，同一张图切换面数或 PBR 后会生成新任务。
            task_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            if self.store.register_task(
                task_id=task_id,
                source_path=str(path.resolve()),
                source_sha256=source_hash,
                model=self.settings.model,
                enable_pbr=self.settings.enable_pbr,
                face_count=self.settings.face_count,
                generate_type=self.settings.generate_type,
            ):
                added += 1
        return len(sources), added

    def _input_sources(self) -> list[Path]:
        input_path = self.settings.input_path
        if input_path.is_file() and input_path.suffix.lower() == ".json":
            return self._sources_from_manifest(input_path)
        if input_path.is_file() and input_path.suffix.lower() in SUPPORTED_SUFFIXES:
            return [input_path.resolve()]
        if input_path.is_dir():
            return sorted(
                path.resolve()
                for path in input_path.rglob("*")
                if path.is_file()
                and path.suffix.lower() in SUPPORTED_SUFFIXES
                and "_mock" not in path.parts
            )
        raise FileNotFoundError(f"3D 输入不存在：{input_path}")

    @staticmethod
    def _sources_from_manifest(manifest_path: Path) -> list[Path]:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 2D 清单：{manifest_path}") from exc
        if not isinstance(payload, list):
            raise ValueError("2D manifest.json 必须是数组")
        paths: set[Path] = set()
        for row in payload:
            if not isinstance(row, dict) or str(row.get("status")) != "complete":
                continue
            value = row.get("output_path")
            if not value:
                continue
            path = Path(str(value)).resolve()
            if path.exists() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                paths.add(path)
        return sorted(paths)

    def run(self) -> dict[str, int]:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        tasks = self.store.runnable_tasks()
        if tasks:
            with ThreadPoolExecutor(max_workers=self.settings.concurrency) as executor:
                futures = {executor.submit(self._process_task, task): task for task in tasks}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        self.store.set_failed(
                            task.task_id,
                            error_code="unexpected_error",
                            error_message=str(exc),
                        )
        self.export_manifest()
        return self.store.summary()

    def _process_task(self, task: ModelTask) -> None:
        source_path = Path(task.source_path)
        if not source_path.exists():
            self.store.set_failed(
                task.task_id,
                error_code="source_missing",
                error_message=f"源资产图不存在：{source_path}",
            )
            return

        job_id = task.remote_job_id
        if not job_id:
            # 提交前先落库为 submitting；进程中断时可以提醒用户核对是否已经计费。
            self.store.set_submitting(task.task_id)
            try:
                submitted = self.client.submit(source_path)
            except (HunyuanApiError, OSError, ValueError) as exc:
                self.store.set_failed(
                    task.task_id,
                    error_code=getattr(exc, "error_code", None) or "submit_error",
                    error_message=str(exc),
                    request_id=getattr(exc, "request_id", None),
                )
                return
            job_id = submitted.job_id
            self.store.set_submitted(
                task.task_id,
                job_id=job_id,
                request_id=submitted.request_id,
            )

        # 有远端任务 ID 就只轮询，不重复提交；这是 3D 流程可断点续跑的关键。
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        last_network_error: str | None = None
        while time.monotonic() < deadline:
            try:
                result = self.client.query(job_id)
                last_network_error = None
            except HunyuanApiError as exc:
                if not exc.retryable:
                    self.store.set_failed(
                        task.task_id,
                        error_code=exc.error_code or "query_error",
                        error_message=str(exc),
                        request_id=exc.request_id,
                    )
                    return
                last_network_error = str(exc)
                time.sleep(self.settings.poll_interval_seconds)
                continue

            if result.status in {"completed", "done"}:
                self._download_result(task, source_path, result)
                return
            if result.status in {"failed", "fail", "error"}:
                self.store.set_failed(
                    task.task_id,
                    error_code=result.error_code or "remote_failed",
                    error_message=result.error_message or "混元 3D 任务失败",
                    request_id=result.request_id,
                )
                return
            self.store.set_running(task.task_id, request_id=result.request_id)
            time.sleep(self.settings.poll_interval_seconds)

        message = "本地轮询超时；远端任务 ID 已保留，下次运行会继续查询，不会重复提交"
        if last_network_error:
            message += f"。最后一次网络错误：{last_network_error}"
        self.store.set_waiting(task.task_id, message)

    def _download_result(self, task: ModelTask, source_path: Path, result: Queried3DJob) -> None:
        # 远端“完成”不等于本地资产已完成：还要找到 GLB、下载、校验文件头并原子保存。
        glb = next((item for item in result.files if item.file_type == "glb"), None)
        if glb is None:
            self.store.set_failed(
                task.task_id,
                error_code="glb_missing",
                error_message="混元任务完成但响应中没有 GLB 文件",
                request_id=result.request_id,
            )
            return
        try:
            model_bytes = self._download_with_retries(glb.url)
            if not model_bytes.startswith(b"glTF"):
                raise ValueError("下载结果不是有效的 GLB 文件")
            stem = sanitize_filename(source_path.stem)
            output_path = self.settings.output_dir / f"{stem}__{task.task_id[:8]}.glb"
            atomic_write(output_path, model_bytes)

            preview_path: Path | None = None
            if glb.preview_url:
                preview_bytes = self._download_with_retries(glb.preview_url)
                preview_path = self.settings.output_dir / f"{stem}__{task.task_id[:8]}__preview.jpg"
                atomic_write(preview_path, preview_bytes)
        except (HunyuanApiError, OSError, ValueError) as exc:
            self.store.set_waiting(
                task.task_id,
                f"远端任务已完成但结果下载失败；下次运行将使用同一任务 ID 重试下载：{exc}",
            )
            return

        self.store.set_complete(
            task.task_id,
            output_path=str(output_path),
            preview_path=str(preview_path) if preview_path else None,
            request_id=result.request_id,
            credits_consumed=result.credits_consumed,
        )

    def _download_with_retries(self, url: str) -> bytes:
        last_error: HunyuanApiError | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                return self.client.download(url)
            except HunyuanApiError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.settings.max_retries:
                    break
                time.sleep(min(2**attempt, 30))
        assert last_error is not None
        raise last_error

    def export_manifest(self) -> None:
        rows = self.store.manifest_rows()
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.settings.output_dir / "manifest-3d.json",
            json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        csv_path = self.settings.output_dir / "manifest-3d.csv"
        if not rows:
            atomic_write(csv_path, b"")
            return
        temp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, csv_path)
