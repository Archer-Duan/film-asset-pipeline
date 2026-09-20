"""Business-day output paths and readable, collision-resistant delivery names."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .pipeline import sanitize_filename

OFFICE_TIMEZONE = timezone(timedelta(hours=8))
KINDS = {
    "edit": "补全图",
    "model": "单图模型",
    "full": "补图生模",
    "multiview": "多视图模型",
}


def task_prefix(task, role):
    stamp = datetime.fromisoformat(task["created"]).astimezone(OFFICE_TIMEZONE)
    source = Path(task.get("original") or task["source"]).stem.split("__", 1)[0]
    if source.startswith("_cgi-bin_"):
        source = "参考资产"
    title = sanitize_filename(task.get("title") or source)[:48]
    kind = KINDS.get(task["kind"], sanitize_filename(task["kind"])[:24])
    folder = stamp.strftime("%Y-%m-%d") + ("_3d" if role == "model" else "_2d")
    stem = f"{title}__{kind}__{stamp:%H%M%S}__{task['id'][:8]}"
    return f"{folder}/{stem}"


def result_relative_path(task, role, index, suffix):
    label = "模型" if role == "model" else "图片"
    return Path(f"{task_prefix(task, role)}__{label}-{index + 1:02d}{suffix.lower()}")


def download_name(asset, role, suffix):
    title = sanitize_filename(asset.get("title") or "资产")[:64]
    return f"{title}__{'模型' if role == 'model' else '图片'}__{asset['asset_id'][-12:]}{suffix.lower()}"
