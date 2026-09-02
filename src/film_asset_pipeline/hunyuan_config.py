from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import load_dotenv
from .credentials import get_secret, load_preferences


@dataclass(frozen=True)
class HunyuanSettings:
    root_dir: Path
    input_path: Path
    output_dir: Path
    state_db: Path
    api_style: str = "tencentcloud_sdk"
    submit_endpoint: str = "https://api.ai3d.cloud.tencent.com/v1/ai3d/submit"
    query_endpoint: str = "https://api.ai3d.cloud.tencent.com/v1/ai3d/query"
    api_key: str = ""
    secret_id: str = ""
    secret_key: str = ""
    cloud_endpoint: str = "ai3d.tencentcloudapi.com"
    cloud_version: str = "2025-05-13"
    cloud_region: str = "ap-guangzhou"
    model: str = "hy-3d-3.1"
    minimal_request: bool = False
    enable_pbr: bool = True
    face_count: int = 1_000_000
    generate_type: str = "Normal"
    concurrency: int = 1
    poll_interval_seconds: int = 15
    job_timeout_seconds: int = 1800
    request_timeout_seconds: int = 120
    max_retries: int = 3

    def with_overrides(self, **values: Any) -> "HunyuanSettings":
        return replace(self, **{key: value for key, value in values.items() if value is not None})

    def validate(self, require_key: bool = True) -> None:
        if self.api_style not in {"tencentcloud_sdk", "ai3d_openai", "tokenhub"}:
            raise ValueError("api_style 必须是 tencentcloud_sdk、ai3d_openai 或 tokenhub")
        if self.model not in {"hy-3d-3.0", "hy-3d-3.1"}:
            raise ValueError("3D model 必须是 hy-3d-3.0 或 hy-3d-3.1")
        if self.generate_type not in {"Normal", "Geometry"}:
            raise ValueError("当前 MVP 的 generate_type 只支持 Normal 或 Geometry")
        if not 3_000 <= self.face_count <= 1_500_000:
            raise ValueError("face_count 必须在 3000 到 1500000 之间")
        if self.concurrency < 1:
            raise ValueError("3D concurrency 必须大于 0")
        if self.poll_interval_seconds < 1 or self.job_timeout_seconds < 1:
            raise ValueError("3D 轮询间隔和任务超时必须大于 0")
        if self.request_timeout_seconds < 1 or self.max_retries < 0:
            raise ValueError("3D 请求超时必须大于 0，重试次数不能小于 0")
        if require_key and self.api_style == "tencentcloud_sdk":
            if not self.secret_id or not self.secret_key:
                raise ValueError(
                    "缺少 TENCENTCLOUD_SECRET_ID 或 TENCENTCLOUD_SECRET_KEY；"
                    "请写入本地 .env，不要通过聊天发送"
                )
        elif require_key and not self.api_key:
            raise ValueError("缺少 HUNYUAN_3D_API_KEY；请写入本地 .env，不要通过聊天发送")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def _resolve(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (root / candidate).resolve()


def load_hunyuan_settings(config_path: Path) -> HunyuanSettings:
    config_path = config_path.resolve()
    root = config_path.parent
    load_dotenv(root / ".env")
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}；先运行 film-assets init")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    paths = _section(data, "paths")
    hunyuan = _section(data, "hunyuan")
    preferences = load_preferences(root)
    api_style = str(preferences.get("hunyuan_api_style") or hunyuan.get("api_style", "tencentcloud_sdk"))
    return HunyuanSettings(
        root_dir=root,
        input_path=_resolve(root, str(paths.get("model_input", "data/output/manifest.json"))),
        output_dir=_resolve(root, str(paths.get("model_output_dir", "data/models"))),
        state_db=_resolve(root, str(paths.get("model_state_db", "data/state/models.sqlite3"))),
        api_style=api_style,
        submit_endpoint=str(
            hunyuan.get("submit_endpoint", "https://api.ai3d.cloud.tencent.com/v1/ai3d/submit")
        ),
        query_endpoint=str(
            hunyuan.get("query_endpoint", "https://api.ai3d.cloud.tencent.com/v1/ai3d/query")
        ),
        api_key=(
            get_secret("HUNYUAN_3D_API_KEY", root)
            or os.getenv("TENCENT_TOKENHUB_API_KEY", "").strip()
        ),
        secret_id=get_secret("TENCENTCLOUD_SECRET_ID", root),
        secret_key=get_secret("TENCENTCLOUD_SECRET_KEY", root),
        cloud_endpoint=str(hunyuan.get("cloud_endpoint", "ai3d.tencentcloudapi.com")),
        cloud_version=str(hunyuan.get("cloud_version", "2025-05-13")),
        cloud_region=str(hunyuan.get("cloud_region", "ap-guangzhou")),
        model=str(hunyuan.get("model", "hy-3d-3.1")),
        minimal_request=bool(hunyuan.get("minimal_request", False)),
        enable_pbr=bool(hunyuan.get("enable_pbr", True)),
        face_count=int(hunyuan.get("face_count", 1_000_000)),
        generate_type=str(hunyuan.get("generate_type", "Normal")),
        concurrency=int(hunyuan.get("concurrency", 1)),
        poll_interval_seconds=int(hunyuan.get("poll_interval_seconds", 15)),
        job_timeout_seconds=int(hunyuan.get("job_timeout_seconds", 1800)),
        request_timeout_seconds=int(hunyuan.get("request_timeout_seconds", 120)),
        max_retries=int(hunyuan.get("max_retries", 3)),
    )
