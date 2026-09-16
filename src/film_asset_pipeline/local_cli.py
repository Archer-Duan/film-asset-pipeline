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
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--kind", choices=("edit", "model", "full"), default="edit")
    run.add_argument("--prompt", default="")
    run.add_argument("--wait", action="store_true", help="启动队列执行器并等待本批完成")
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
    if args.command == "status-local":
        print(json.dumps(engine.public_tasks(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "retry-local":
        print(json.dumps(engine.retry(args.task_id, "local"), ensure_ascii=False))
        return 0
    if args.command == "run-local":
        sources = (
            sorted(
                p
                for p in args.input.iterdir()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
            )
            if args.input.is_dir()
            else [args.input]
        )
        group = engine.submit(sources, args.kind, args.prompt)
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
