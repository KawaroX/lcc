from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env import load_env

DEFAULT_BASE_URL = "https://booking.lib.buaa.edu.cn"
CONFIG_FILENAME = ".lcc.json"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthConfig:
    token: str
    cookie: str
    base_url: str = DEFAULT_BASE_URL
    verify_ssl: bool = True
    default_area_id: str | None = None


def _config_path() -> Path:
    return Path.cwd() / CONFIG_FILENAME


def save_auth(
    *,
    token: str,
    cookie: str,
    base_url: str | None = None,
    verify_ssl: bool = True,
    default_area_id: str | None = None,
) -> None:
    token = (token or "").strip()
    cookie = (cookie or "").strip()
    if not token:
        raise ConfigError("token 为空")
    if not cookie:
        raise ConfigError("cookie 为空")

    data = {
        "token": token,
        "cookie": cookie,
        "base_url": (base_url or DEFAULT_BASE_URL).strip(),
        "verify_ssl": bool(verify_ssl),
        "default_area_id": (str(default_area_id).strip() if default_area_id is not None else None),
    }
    _config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear_auth() -> None:
    path = _config_path()
    if path.exists():
        path.unlink()


def _load_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ConfigError(f"{CONFIG_FILENAME} 不是合法 JSON: {e}") from e


def load_auth() -> AuthConfig:
    file_data = _load_file(_config_path())
    env_file = load_env()

    token = (os.environ.get("LCC_TOKEN") or env_file.get("LCC_TOKEN") or file_data.get("token") or "").strip()
    cookie = (os.environ.get("LCC_COOKIE") or env_file.get("LCC_COOKIE") or file_data.get("cookie") or "").strip()
    base_url = (os.environ.get("LCC_BASE_URL") or env_file.get("LCC_BASE_URL") or file_data.get("base_url") or DEFAULT_BASE_URL).strip()
    env_insecure = (os.environ.get("LCC_INSECURE") or env_file.get("LCC_INSECURE") or "").strip()
    if env_insecure:
        verify_ssl = False
    else:
        verify_ssl = bool(file_data.get("verify_ssl", True))

    default_area_id = (
        os.environ.get("LCC_DEFAULT_AREA_ID")
        or env_file.get("LCC_DEFAULT_AREA_ID")
        or file_data.get("default_area_id")
        or ""
    ).strip() or None

    if not token:
        raise ConfigError("缺少 token：请先运行 `lcc auth login` / `lcc auth set`，或在 .env 里放账号让 CLI 自动登录")
    if not cookie:
        raise ConfigError("缺少 cookie：请先运行 `lcc auth login` / `lcc auth set`")

    return AuthConfig(
        token=token,
        cookie=cookie,
        base_url=base_url,
        verify_ssl=verify_ssl,
        default_area_id=default_area_id,
    )


def load_auth_loose() -> AuthConfig:
    """
    Like load_auth(), but allows missing token/cookie (for bootstrapping auto-login).
    """
    file_data = _load_file(_config_path())
    env_file = load_env()
    token = (os.environ.get("LCC_TOKEN") or env_file.get("LCC_TOKEN") or file_data.get("token") or "").strip()
    cookie = (os.environ.get("LCC_COOKIE") or env_file.get("LCC_COOKIE") or file_data.get("cookie") or "").strip()
    base_url = (os.environ.get("LCC_BASE_URL") or env_file.get("LCC_BASE_URL") or file_data.get("base_url") or DEFAULT_BASE_URL).strip()
    env_insecure = (os.environ.get("LCC_INSECURE") or env_file.get("LCC_INSECURE") or "").strip()
    verify_ssl = False if env_insecure else bool(file_data.get("verify_ssl", True))
    default_area_id = (
        os.environ.get("LCC_DEFAULT_AREA_ID")
        or env_file.get("LCC_DEFAULT_AREA_ID")
        or file_data.get("default_area_id")
        or ""
    ).strip() or None
    return AuthConfig(
        token=token,
        cookie=cookie,
        base_url=base_url,
        verify_ssl=verify_ssl,
        default_area_id=default_area_id,
    )


def update_defaults(*, default_area_id: str | None = None) -> None:
    """
    Update defaults in .lcc.json without touching token/cookie.
    """
    path = _config_path()
    data = _load_file(path)
    if default_area_id is not None:
        data["default_area_id"] = str(default_area_id).strip() or None
    if not data:
        raise ConfigError("未找到 .lcc.json：请先运行一次 `lcc auth set` 或 `lcc auth login`")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_cached_area_tree(*, max_age_sec: int = 86400) -> dict | None:
    """
    Return cached area tree if fresh (default TTL 24h). None otherwise.
    """
    import time as _t
    data = _load_file(_config_path())
    cache = data.get("area_tree_cache")
    if not isinstance(cache, dict):
        return None
    fetched_at = cache.get("fetched_at")
    tree = cache.get("tree")
    if not isinstance(fetched_at, (int, float)) or not isinstance(tree, dict):
        return None
    if _t.time() - float(fetched_at) > max_age_sec:
        return None
    return tree


def cache_area_tree(tree: dict) -> None:
    """
    Persist area tree cache into .lcc.json.
    """
    import time as _t
    path = _config_path()
    data = _load_file(path)
    data["area_tree_cache"] = {"fetched_at": int(_t.time()), "tree": tree}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_cached_segment(*, area_id: str, start_time: str, end_time: str) -> str | None:
    """
    Return cached segment for a (area_id, start_time, end_time) triple.
    """
    data = _load_file(_config_path())
    cache = data.get("segment_cache")
    if not isinstance(cache, dict):
        return None
    key = f"{str(area_id).strip()}|{str(start_time).strip()}|{str(end_time).strip()}"
    seg = cache.get(key)
    return str(seg).strip() if seg is not None and str(seg).strip() else None


def cache_segment(*, area_id: str, start_time: str, end_time: str, segment: str) -> None:
    """
    Persist segment cache into .lcc.json.
    """
    segment = str(segment).strip()
    if not segment:
        return
    path = _config_path()
    data = _load_file(path)
    cache = data.get("segment_cache")
    if not isinstance(cache, dict):
        cache = {}
    key = f"{str(area_id).strip()}|{str(start_time).strip()}|{str(end_time).strip()}"
    cache[key] = segment
    data["segment_cache"] = cache
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_pomo_state(state: dict) -> None:
    """
    Save pomodoro daemon state to .lcc.json.
    """
    path = _config_path()
    data = _load_file(path)
    data["pomo_daemon"] = state
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_pomo_state() -> dict | None:
    """
    Load pomodoro daemon state from .lcc.json.
    Returns None if no state exists.
    """
    data = _load_file(_config_path())
    state = data.get("pomo_daemon")
    if isinstance(state, dict):
        return state
    return None


def clear_pomo_state() -> None:
    """
    Remove pomodoro daemon state from .lcc.json.
    """
    path = _config_path()
    data = _load_file(path)
    if "pomo_daemon" in data:
        del data["pomo_daemon"]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_pomo_running() -> bool:
    """
    Check if pomodoro daemon is running based on saved PID.
    Returns True if PID exists and process is alive.
    """
    import os
    import sys

    state = load_pomo_state()
    if not isinstance(state, dict):
        return False

    pid = state.get("pid")
    if not isinstance(pid, int):
        return False

    try:
        if sys.platform == "win32":
            # Windows: try to open process
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            # Unix: send signal 0
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, AttributeError):
        return False
