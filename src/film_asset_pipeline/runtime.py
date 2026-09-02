from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "FilmAssetPipeline"


def user_data_dir() -> Path:
    """Return a per-user writable directory without requiring third-party packages."""
    override = os.getenv("FILM_ASSET_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (base / APP_DIR_NAME).resolve()


def default_config_path() -> Path:
    explicit = os.getenv("FILM_ASSET_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    project_config = Path.cwd() / "config.toml"
    if project_config.is_file():
        # Preserve existing development installations and their current assets.
        return project_config.resolve()
    return user_data_dir() / "config.toml"


def ensure_runtime_directories(root: Path) -> None:
    for relative in ("data/input", "data/output", "data/models", "data/state", "logs"):
        (root / relative).mkdir(parents=True, exist_ok=True)
