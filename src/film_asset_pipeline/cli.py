from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .ark import ArkImageClient
from .config import Settings, load_settings
from .hunyuan import Hunyuan3DClient
from .hunyuan_config import HunyuanSettings, load_hunyuan_settings
from .model_pipeline import Mock3DClient, ModelBatchPipeline
from .model_state import ModelStateStore
from .pipeline import BatchPipeline, MockImageClient
from .runtime import ensure_runtime_directories
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="film-assets",
        description="批量把电影静帧生成干净背景资产图",
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="配置文件路径，默认使用当前目录的 config.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化配置和数据目录")

    run_parser = subparsers.add_parser("run", help="扫描输入目录并执行待处理任务")
    run_parser.add_argument("--input", type=Path, help="覆盖输入目录")
    run_parser.add_argument("--output", type=Path, help="覆盖输出目录")
    run_parser.add_argument("--mode", choices=("group", "single"), help="group=一图生成一组图，single=一图生成单图")
    run_parser.add_argument("--max-images", type=int, help="组图最多返回数量")
    run_parser.add_argument("--prompt", help="覆盖默认提示词")
    run_parser.add_argument("--concurrency", type=int, help="并发请求数")
    run_parser.add_argument("--mock", action="store_true", help="不调用 API，仅验证本地流程")

    subparsers.add_parser("status", help="查看任务状态汇总")
    retry_parser = subparsers.add_parser("retry", help="把失败任务重置为待重试")
    retry_parser.add_argument("--task-id", help="只重置指定任务，避免批量误重试")

    model_parser = subparsers.add_parser("run-3d", help="把已审核资产图批量生成 GLB 模型")
    model_parser.add_argument("--input", type=Path, help="覆盖 2D manifest.json、单张资产图或资产图目录")
    model_parser.add_argument("--output", type=Path, help="覆盖 3D 输出目录")
    model_parser.add_argument("--model", choices=("hy-3d-3.0", "hy-3d-3.1"), help="混元模型")
    model_parser.add_argument("--face-count", type=int, help="目标面数，3000 到 1500000")
    model_parser.add_argument("--concurrency", type=int, help="3D 并发任务数")
    model_parser.add_argument(
        "--minimal-request",
        action="store_true",
        help="诊断模式：只提交 Model 和图像，使用服务端默认50万面、非PBR参数",
    )
    model_parser.add_argument("--mock", action="store_true", help="不调用 API，仅验证 3D 本地流程")

    subparsers.add_parser("status-3d", help="查看 3D 任务状态汇总")
    retry_model_parser = subparsers.add_parser(
        "retry-3d", help="人工确认后重置失败的 3D 任务"
    )
    retry_model_parser.add_argument("--task-id", help="只重置指定任务，避免批量误重试")
    return parser


def initialize_project(config_path: Path) -> int:
    config_path = config_path.resolve()
    root = config_path.parent
    example = root / "config.example.toml"
    if not example.exists():
        print(f"缺少示例配置：{example}", file=sys.stderr)
        return 2
    if not config_path.exists():
        shutil.copyfile(example, config_path)
        print(f"已创建配置：{config_path}")
    else:
        print(f"配置已存在，未覆盖：{config_path}")
    ensure_runtime_directories(root)
    print("初始化完成。把静帧放入 data/input，并把新 API Key 写入本地 .env。")
    return 0


def _absolute_or_none(path: Path | None) -> Path | None:
    return path.resolve() if path is not None else None


def run_command(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config))
    settings = settings.with_overrides(
        input_dir=_absolute_or_none(args.input),
        output_dir=_absolute_or_none(args.output),
        mode=args.mode,
        max_images=args.max_images,
        prompt=args.prompt,
        concurrency=args.concurrency,
    )
    if args.mock:
        settings = settings.with_overrides(
            output_dir=settings.output_dir / "_mock",
            state_db=settings.state_db.with_name(
                f"{settings.state_db.stem}-mock{settings.state_db.suffix}"
            ),
        )
    settings.validate(require_key=not args.mock)
    if not settings.input_dir.is_file():
        settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(settings.state_db)
    try:
        interrupted = store.recover_interrupted()
        if interrupted:
            print(
                f"检测到 {interrupted} 个上次中断的任务，已标记为失败；"
                "请确认是否已产生结果或费用，再执行 retry。"
            )
        client = MockImageClient() if args.mock else _real_client(settings)
        pipeline = BatchPipeline(settings, store, client)
        found, added = pipeline.scan()
        print(f"扫描到 {found} 张图片，本次新增 {added} 个任务。")
        summary = pipeline.run()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"结果目录：{settings.output_dir}")
        if settings.input_dir.is_file():
            target_path = settings.input_dir.resolve()
            target_rows = [
                row
                for row in store.manifest_rows()
                if Path(str(row.get("source_path") or "")).resolve() == target_path
                and str(row.get("prompt") or "") == settings.prompt
                and str(row.get("mode") or "") == settings.mode
                and int(row.get("max_images") or 0) == settings.max_images
            ]
            return 0 if target_rows and all(row.get("status") == "complete" for row in target_rows) else 1
        return 1 if summary.get("failed", 0) else 0
    finally:
        store.close()


def _real_client(settings: Settings) -> ArkImageClient:
    return ArkImageClient(
        endpoint=settings.endpoint,
        api_key=settings.api_key,
        model=settings.model,
        size=settings.size,
        response_format=settings.response_format,
        watermark=settings.watermark,
        timeout_seconds=settings.request_timeout_seconds,
    )


def run_3d_command(args: argparse.Namespace) -> int:
    settings = load_hunyuan_settings(Path(args.config)).with_overrides(
        input_path=_absolute_or_none(args.input),
        output_dir=_absolute_or_none(args.output),
        model=args.model,
        face_count=args.face_count,
        concurrency=args.concurrency,
    )
    if args.minimal_request:
        settings = settings.with_overrides(
            minimal_request=True,
            enable_pbr=False,
            face_count=500_000,
            generate_type="Normal",
        )
    if args.mock:
        settings = settings.with_overrides(
            output_dir=settings.output_dir / "_mock",
            state_db=settings.state_db.with_name(
                f"{settings.state_db.stem}-mock{settings.state_db.suffix}"
            ),
        )
    settings.validate(require_key=not args.mock)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    store = ModelStateStore(settings.state_db)
    try:
        interrupted = store.recover_submitting()
        if interrupted:
            print(
                f"检测到 {interrupted} 个提交阶段中断的任务，已标记为失败；"
                "请先核对控制台任务和费用，再执行 retry-3d。"
            )
        client = Mock3DClient() if args.mock else _real_3d_client(settings)
        pipeline = ModelBatchPipeline(settings, store, client)
        found, added = pipeline.scan()
        print(f"扫描到 {found} 张已审核资产图，本次新增 {added} 个 3D 任务。")
        summary = pipeline.run()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"3D 结果目录：{settings.output_dir}")
        return 1 if summary.get("failed", 0) else 0
    finally:
        store.close()


def _real_3d_client(settings: HunyuanSettings) -> Hunyuan3DClient:
    return Hunyuan3DClient(
        api_style=settings.api_style,
        submit_endpoint=settings.submit_endpoint,
        query_endpoint=settings.query_endpoint,
        api_key=settings.api_key,
        secret_id=settings.secret_id,
        secret_key=settings.secret_key,
        cloud_endpoint=settings.cloud_endpoint,
        cloud_version=settings.cloud_version,
        cloud_region=settings.cloud_region,
        model=settings.model,
        minimal_request=settings.minimal_request,
        enable_pbr=settings.enable_pbr,
        face_count=settings.face_count,
        generate_type=settings.generate_type,
        timeout_seconds=settings.request_timeout_seconds,
    )


def status_command(config_path: Path) -> int:
    settings = load_settings(config_path)
    store = StateStore(settings.state_db)
    try:
        print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def retry_command(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config))
    store = StateStore(settings.state_db)
    try:
        count = store.retry_failed(args.task_id)
        print(f"已将 {count} 个失败任务重置为待重试。")
    finally:
        store.close()
    return 0


def status_3d_command(config_path: Path) -> int:
    settings = load_hunyuan_settings(config_path)
    store = ModelStateStore(settings.state_db)
    try:
        print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def retry_3d_command(args: argparse.Namespace) -> int:
    settings = load_hunyuan_settings(Path(args.config))
    store = ModelStateStore(settings.state_db)
    try:
        count = store.retry_failed(args.task_id)
        print(f"已将 {count} 个失败的 3D 任务重置为待重试。")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        if args.command == "init":
            return initialize_project(config_path)
        if args.command == "run":
            return run_command(args)
        if args.command == "status":
            return status_command(config_path)
        if args.command == "retry":
            return retry_command(args)
        if args.command == "run-3d":
            return run_3d_command(args)
        if args.command == "status-3d":
            return status_3d_command(config_path)
        if args.command == "retry-3d":
            return retry_3d_command(args)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
