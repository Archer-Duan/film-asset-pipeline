import getpass
import json
import time
from pathlib import Path

from .local_engine import LocalEngine
from .team_auth import TeamAuth


def add_commands(subparsers):
    run = subparsers.add_parser(
        "run-local", help="通过本地 ComfyUI 补图或生模，沿用持久化队列"
    )
    run.add_argument("--input", type=Path)
    run.add_argument(
        "--inputs", type=Path, help="JSON 对象文件，图片字段名对应本地路径"
    )
    run.add_argument("--parameters", default="{}", help="工作流参数 JSON 对象")
    run.add_argument("--version", help="指定工作流版本")
    run.add_argument(
        "--kind", default="edit", help="工作流 ID，含 multiview 或已启用的导入工作流"
    )
    run.add_argument("--prompt", default="")
    run.add_argument("--name", default="", help="资产名称，用于归档和下载文件名")
    run.add_argument("--wait", action="store_true", help="启动队列执行器并等待本批完成")
    library = subparsers.add_parser("workflows-local", help="列出、导入或启停工作流")
    library.add_argument("--import-file", type=Path)
    library.add_argument("--id")
    library.add_argument("--version")
    action = library.add_mutually_exclusive_group()
    action.add_argument("--enable", action="store_true")
    action.add_argument("--disable", action="store_true")
    subparsers.add_parser("status-local", help="列出本地 ComfyUI 任务")
    retry = subparsers.add_parser("retry-local", help="重试一个本地任务")
    retry.add_argument("--task-id", required=True)
    subparsers.add_parser("worker-local", help="运行本地 GPU 队列执行器")
    member = subparsers.add_parser(
        "member-local", help="创建或重置局域网成员，交互输入密码"
    )
    member.add_argument("--name", required=True)


def run(args):
    root = Path(args.config).resolve().parent
    if args.command == "member-local":
        password = getpass.getpass("Member password (10+ characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("两次密码不一致")
        TeamAuth(root).add_user(args.name, password)
        print("Member saved.")
        return 0
    engine = LocalEngine(root)
    if args.command == "workflows-local":
        if args.import_file:
            engine.library.import_bundle(
                json.loads(args.import_file.read_text("utf-8-sig"))
            )
        if args.enable or args.disable:
            if not args.id or not args.version:
                raise ValueError("启停工作流需指定 --id 与 --version")
            engine.enable_workflow(args.id, args.version, args.enable)
        print(json.dumps(engine.health(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "status-local":
        print(json.dumps(engine.public_tasks(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "retry-local":
        print(json.dumps(engine.retry(args.task_id, "local"), ensure_ascii=False))
        return 0
    if args.command == "run-local":
        if bool(args.input) == bool(args.inputs):
            raise ValueError("请提供 --input 或 --inputs，二选一")
        mapping = None
        if args.inputs:
            mapping = json.loads(args.inputs.read_text("utf-8-sig"))
            if not isinstance(mapping, dict) or not all(
                isinstance(v, str) for v in mapping.values()
            ):
                raise ValueError("--inputs 需要图片字段名到文件路径的 JSON 对象")
            mapping = {
                k: str((args.inputs.resolve().parent / v).resolve())
                for k, v in mapping.items()
            }
        sources = (
            sorted(
                p
                for p in args.input.iterdir()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
            )
            if args.input and args.input.is_dir()
            else [args.input]
        )
        group = engine.submit(
            sources,
            args.kind,
            args.prompt,
            input_sets=[mapping] if mapping else None,
            parameters=json.loads(args.parameters),
            version=args.version,
            title=args.name,
        )
        print(json.dumps(group, ensure_ascii=False), flush=True)
        if not args.wait:
            print("任务已入队；请保持工作台运行，或启动 worker-local。")
            return 0
    engine.start()
    try:
        while True:
            if args.command == "run-local":
                group = engine.group(group["job_id"])
                if group["status"] in ("complete", "failed"):
                    print(json.dumps(group, ensure_ascii=False), flush=True)
                    return int(group["status"] == "failed")
            time.sleep(3)
    except KeyboardInterrupt:
        return 130
    finally:
        engine.close()
