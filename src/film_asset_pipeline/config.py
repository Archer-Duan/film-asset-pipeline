from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .credentials import get_secret

DEFAULT_PROMPT = (
    "输入可能是带网页界面和文字说明的电影数据库截图。忽略网页标题、镜号、时间码、片名、说明文字、字幕、边框和播放器控件，"
    "只分析截图中间的电影画面。"
    "识别画面中央、叙事焦点和视觉显著区域中适合作为电影工业数字资产的1至3件核心重要物品。"
    "为每件物品分别生成一张独立图片，每张图只能出现一个物品，不要拼图。"
    "保持原物品的造型、年代、材质、颜色、磨损和设计语言，合理补全遮挡或画面外缺失结构。"
    "物品完整、无遮挡、居中，使用纯白或浅灰色干净背景，不要人物、文字、水印、边框、环境和其他道具，"
    "不要复杂投影，可保留轻微自然接触阴影。输出清晰、写实、适合作为图生3D参考图的完整资产视图。"
)


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    input_dir: Path
    output_dir: Path
    state_db: Path
    endpoint: str = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    model: str = "doubao-seedream-5-0-lite-260128"
    api_key: str = ""
    size: str = "2K"
    response_format: str = "b64_json"
    watermark: bool = False
    mode: str = "group"
    max_images: int = 3
    prompt: str = DEFAULT_PROMPT
    concurrency: int = 2
    request_timeout_seconds: int = 600
    max_retries: int = 3

    def with_overrides(self, **values: Any) -> Settings:
        # CLI 只传入实际指定的参数；None 不覆盖 TOML 中的默认值。
        clean = {key: value for key, value in values.items() if value is not None}
        return replace(self, **clean)

    def validate(self, require_key: bool = True) -> None:
        if self.mode not in {"group", "single"}:
            raise ValueError("mode 必须是 group 或 single")
        if not 1 <= self.max_images <= 15:
            raise ValueError("max_images 必须在 1 到 15 之间")
        if self.concurrency < 1:
            raise ValueError("concurrency 必须大于 0")
        if self.request_timeout_seconds < 1:
            raise ValueError("request_timeout_seconds 必须大于 0")
        if self.max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if require_key and not self.api_key:
            raise ValueError("缺少 ARK_API_KEY；请把新 Key 写入本地 .env，不要写入 config.toml")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def _resolve(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (root / candidate).resolve()


def load_settings(config_path: Path) -> Settings:
    # 配置文件负责非敏感参数，密钥从同目录 .env / 环境变量读取，避免写入 TOML。
    config_path = config_path.resolve()
    root = config_path.parent
    load_dotenv(root / ".env")
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}；先运行 film-assets init")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    paths = _section(data, "paths")
    ark = _section(data, "ark")
    pipeline = _section(data, "pipeline")
    settings = Settings(
        root_dir=root,
        input_dir=_resolve(root, str(paths.get("input_dir", "data/input"))),
        output_dir=_resolve(root, str(paths.get("output_dir", "data/output"))),
        state_db=_resolve(root, str(paths.get("state_db", "data/state/pipeline.sqlite3"))),
        endpoint=str(ark.get("endpoint", Settings.endpoint)),
        model=str(ark.get("model", Settings.model)),
        api_key=get_secret("ARK_API_KEY", root),
        size=str(ark.get("size", "2K")),
        response_format=str(ark.get("response_format", "b64_json")),
        watermark=bool(ark.get("watermark", False)),
        mode=str(ark.get("mode", "group")),
        max_images=int(ark.get("max_images", 3)),
        prompt=str(ark.get("prompt", DEFAULT_PROMPT)),
        concurrency=int(pipeline.get("concurrency", 2)),
        request_timeout_seconds=int(pipeline.get("request_timeout_seconds", 600)),
        max_retries=int(pipeline.get("max_retries", 3)),
    )
    return settings
