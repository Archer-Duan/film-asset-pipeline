from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from .ark import ArkApiError, GeneratedImage, GenerationResponse
from .config import Settings
from .state import StateStore, TaskRecord


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class ImageClient(Protocol):
    def generate(self, source_path: Path, prompt: str, mode: str, max_images: int) -> GenerationResponse: ...


class MockImageClient:
    """Copies input bytes to outputs so the local workflow can be tested for free."""

    def generate(self, source_path: Path, prompt: str, mode: str, max_images: int) -> GenerationResponse:
        content = source_path.read_bytes()
        extension = source_path.suffix.lower()
        count = min(max_images, 2) if mode == "group" else 1
        images = [GeneratedImage(content=content, extension=extension) for _ in range(count)]
        return GenerationResponse(images=images, request_id="mock-request", model="mock", raw_usage=None)


class BatchPipeline:
    def __init__(self, settings: Settings, store: StateStore, client: ImageClient) -> None:
        self.settings = settings
        self.store = store
        self.client = client

    def scan(self) -> tuple[int, int]:
        # 扫描阶段只负责“发现并登记任务”，不调用模型；这样可以先检查任务身份是否已存在。
        if self.settings.input_dir.is_file():
            candidates = (
                [self.settings.input_dir]
                if self.settings.input_dir.suffix.lower() in SUPPORTED_SUFFIXES
                else []
            )
        else:
            self.settings.input_dir.mkdir(parents=True, exist_ok=True)
            candidates = sorted(
                path
                for path in self.settings.input_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
            )
        added = 0
        for path in candidates:
            source_hash = sha256_file(path)
            identity = "\0".join(
                [str(path.resolve()), source_hash, self.settings.prompt, self.settings.mode, str(self.settings.max_images)]
            )
            # 把输入内容和生成参数纳入 ID，保证同一组合只生成一次，参数变化则产生新任务。
            task_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            if self.store.register_task(
                task_id=task_id,
                source_path=str(path.resolve()),
                source_sha256=source_hash,
                prompt=self.settings.prompt,
                mode=self.settings.mode,
                max_images=self.settings.max_images,
            ):
                added += 1
        return len(candidates), added

    def run(self) -> dict[str, int]:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        tasks = self.store.runnable_tasks()
        if not tasks:
            # 即使没有新任务也要刷新清单，让清单与 SQLite 当前状态保持一致。
            self.export_manifests()
            return self.store.summary()

        # 每个任务独立提交给线程池；SQLite 自身通过锁保护，单个任务失败不会中断整批。
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as executor:
            futures = {executor.submit(self._process_task, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as exc:  # Last-resort guard; individual errors are handled below.
                    self.store.set_failed(
                        task.task_id,
                        error_code="unexpected_error",
                        error_message=str(exc),
                    )
        self.export_manifests()
        return self.store.summary()

    def _process_task(self, task: TaskRecord) -> None:
        source_path = Path(task.source_path)
        self.store.set_running(task.task_id)
        if not source_path.exists():
            self.store.set_failed(
                task.task_id,
                error_code="source_missing",
                error_message=f"源文件不存在：{source_path}",
            )
            return

        response: GenerationResponse | None = None
        last_error: ArkApiError | None = None
        # 只有服务端明确告诉我们“值得重试”时才重试，鉴权、参数和审核错误直接失败。
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.client.generate(source_path, task.prompt, task.mode, task.max_images)
                last_error = None
                break
            except ArkApiError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.settings.max_retries:
                    break
                time.sleep(min(2 ** attempt, 30))
            except (OSError, ValueError) as exc:
                self.store.set_failed(
                    task.task_id,
                    error_code="local_input_error",
                    error_message=str(exc),
                )
                return

        if response is None:
            assert last_error is not None
            self.store.set_failed(
                task.task_id,
                error_code=last_error.error_code or "ark_error",
                error_message=str(last_error),
                request_id=last_error.request_id,
            )
            return

        try:
            # 先写文件，再写 outputs 记录；避免数据库指向一个尚未完整落盘的文件。
            for index, image in enumerate(response.images, start=1):
                output_path = self._output_path(task, source_path, index, image.extension)
                atomic_write(output_path, image.content)
                self.store.add_output(
                    task_id=task.task_id,
                    result_index=index,
                    output_path=str(output_path),
                    sha256=hashlib.sha256(image.content).hexdigest(),
                    width=image.width,
                    height=image.height,
                )
        except OSError as exc:
            self.store.set_failed(
                task.task_id,
                error_code="output_write_error",
                error_message=str(exc),
                request_id=response.request_id,
            )
            return

        self.store.set_complete(
            task.task_id,
            request_id=response.request_id,
            model=response.model,
            usage=response.raw_usage,
        )

    def _output_path(self, task: TaskRecord, source_path: Path, index: int, extension: str) -> Path:
        safe_stem = sanitize_filename(source_path.stem.split("__", 1)[0])
        suffix = extension if extension.startswith(".") else f".{extension}"
        return self.settings.output_dir / f"{safe_stem}__2d-{index:02d}__{task.task_id[:8]}{suffix}"

    def export_manifests(self) -> None:
        rows = self.store.manifest_rows()
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.settings.output_dir / "manifest.json"
        csv_path = self.settings.output_dir / "manifest.csv"
        atomic_write(json_path, json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"))
        if rows:
            temp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            os.replace(temp_path, csv_path)
        else:
            atomic_write(csv_path, b"")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_filename(value: str) -> str:
    invalid = '<>:"/\\|?*'
    result = "".join("_" if character in invalid or ord(character) < 32 else character for character in value)
    result = result.strip(" .")
    return result[:120] or "asset"


def atomic_write(path: Path, content: bytes) -> None:
    # 临时文件写完后再替换，进程中断时不会留下半截正式输出。
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(content)
    os.replace(temp_path, path)
