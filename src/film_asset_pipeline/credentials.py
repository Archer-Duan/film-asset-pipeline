from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


SERVICE_NAME = "film-asset-pipeline"
SECRET_NAMES = (
    "ARK_API_KEY",
    "HUNYUAN_3D_API_KEY",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
)


def _keyring() -> Any | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    return keyring


def get_secret(name: str, config_root: Path) -> str:
    if name not in SECRET_NAMES:
        raise ValueError(f"不支持的凭据名称：{name}")
    environment_value = os.getenv(name, "").strip()
    if environment_value:
        return environment_value
    backend = _keyring()
    if backend is not None:
        try:
            value = backend.get_password(SERVICE_NAME, name)
        except Exception:
            value = None
        if value:
            return str(value).strip()
    return ""


def set_secret(name: str, value: str, config_root: Path) -> str:
    if name not in SECRET_NAMES:
        raise ValueError(f"不支持的凭据名称：{name}")
    clean = value.strip()
    if not clean:
        raise ValueError("密钥不能为空")
    backend = _keyring()
    if backend is not None:
        try:
            backend.set_password(SERVICE_NAME, name, clean)
        except Exception:
            pass
        else:
            _update_env_file(config_root / ".env", name, None)
            os.environ[name] = clean
            return "system_keyring"
    _update_env_file(config_root / ".env", name, clean)
    os.environ[name] = clean
    return "local_env_file"


def delete_secret(name: str, config_root: Path) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"不支持的凭据名称：{name}")
    backend = _keyring()
    if backend is not None:
        try:
            backend.delete_password(SERVICE_NAME, name)
        except Exception:
            pass
    _update_env_file(config_root / ".env", name, None)
    os.environ.pop(name, None)


def masked(value: str) -> str | None:
    clean = value.strip()
    if not clean:
        return None
    tail = clean[-4:] if len(clean) >= 4 else clean
    return f"****{tail}"


def preferences_path(config_root: Path) -> Path:
    return config_root / "user-settings.json"


def load_preferences(config_root: Path) -> dict[str, Any]:
    path = preferences_path(config_root)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preferences(config_root: Path, values: dict[str, Any]) -> None:
    path = preferences_path(config_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_preferences(config_root)
    current.update(values)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _update_env_file(path: Path, name: str, value: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if path.is_file():
        existing = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{name}="
    lines = [line for line in existing if not line.strip().startswith(prefix)]
    if value is not None:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{name}="{escaped}"')
    path.write_text("\n".join(lines).rstrip() + ("\n" if lines else ""), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
