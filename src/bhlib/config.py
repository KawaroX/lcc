from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env import load_env

DEFAULT_BASE_URL = "https://booking.lib.buaa.edu.cn"
KEYRING_SERVICE = "bhlib"
PASSWORD_STORAGE_KEYRING = "keyring"
PASSWORD_STORAGE_PLAIN = "plain"

LEGACY_CONFIG_DIR = Path.home() / ".bhlib"
LEGACY_CONFIG_FILE = LEGACY_CONFIG_DIR / "config.json"

try:
    # platformdirs picks the OS-conventional config directory:
    # - Windows: %APPDATA%\bhlib
    # - macOS:   ~/Library/Application Support/bhlib
    # - Linux:   ~/.config/bhlib (or $XDG_CONFIG_HOME/bhlib)
    from platformdirs import user_config_path as _user_config_path

    CONFIG_DIR = _user_config_path("bhlib", roaming=True)
except Exception:  # pragma: no cover
    # Fallback for environments without platformdirs.
    CONFIG_DIR = LEGACY_CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "config.json"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthConfig:
    token: str
    cookie: str
    base_url: str = DEFAULT_BASE_URL
    verify_ssl: bool = True
    default_area_id: str | None = None
    seat_format: str | None = None  # "map" or "list"
    username: str | None = None
    password: str | None = None
    password_storage: str | None = None  # "keyring" or "plain"


def _config_path() -> Path:
    _maybe_migrate_legacy_config()
    return CONFIG_FILE


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _write(data: dict[str, Any]) -> None:
    _maybe_migrate_legacy_config()
    _ensure_dir()
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def _load_file() -> dict[str, Any]:
    _maybe_migrate_legacy_config()
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ConfigError(f"{CONFIG_FILE} 不是合法 JSON: {e}") from e


def _keyring_account(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise ConfigError("保存到系统凭据库需要 username")
    return username


def _load_keyring_module():
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as e:
        raise ConfigError(
            "当前环境没有安装 keyring，无法使用系统凭据库；"
            "请重新安装/升级 bhlib，或用 `bhlib login --plain-password` 使用明文兜底。"
        ) from e
    return keyring


def _save_password_keyring(*, username: str, password: str) -> None:
    keyring = _load_keyring_module()
    try:
        keyring.set_password(KEYRING_SERVICE, _keyring_account(username), password)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(
            "无法把密码保存到系统凭据库；"
            "如果当前系统/桌面环境没有可用 keyring，可用 `bhlib login --plain-password` 使用明文兜底。"
        ) from e


def _load_password_keyring(*, username: str) -> str | None:
    try:
        keyring = _load_keyring_module()
        password = keyring.get_password(KEYRING_SERVICE, _keyring_account(username))
    except Exception:
        return None
    return password or None


def _delete_password_keyring(*, username: str) -> None:
    keyring = _load_keyring_module()
    try:
        keyring.delete_password(KEYRING_SERVICE, _keyring_account(username))
    except Exception:
        return


def _maybe_migrate_legacy_config() -> None:
    """One-time migration from legacy ~/.bhlib/config.json to the platform config dir.

    This runs on both read and write paths. If the new path does not exist but the
    legacy path does, copy the legacy file to the new location.
    """
    try:
        if CONFIG_FILE.exists():
            return
        if LEGACY_CONFIG_FILE.exists() and CONFIG_FILE != LEGACY_CONFIG_FILE:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                LEGACY_CONFIG_FILE.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except OSError:
                pass
    except OSError:
        # Best-effort only; never block normal operation.
        return


def save_auth(
    *,
    token: str,
    cookie: str,
    base_url: str | None = None,
    verify_ssl: bool = True,
    default_area_id: str | None = None,
    username: str | None = None,
    password: str | None = None,
    password_storage: str | None = None,
) -> None:
    token = (token or "").strip()
    cookie = (cookie or "").strip()
    if not token:
        raise ConfigError("token 为空")
    if not cookie:
        raise ConfigError("cookie 为空")

    data = _load_file()
    data["token"] = token
    data["cookie"] = cookie
    data["base_url"] = (base_url or data.get("base_url") or DEFAULT_BASE_URL).strip()
    data["verify_ssl"] = bool(verify_ssl)
    if default_area_id is not None:
        data["default_area_id"] = str(default_area_id).strip() or None
    if username is not None:
        data["username"] = username.strip()
    if password is not None:
        storage = (password_storage or data.get("password_storage") or PASSWORD_STORAGE_KEYRING)
        storage = str(storage).strip().lower()
        username_for_password = (username or data.get("username") or "").strip()
        if storage == PASSWORD_STORAGE_KEYRING:
            _save_password_keyring(username=username_for_password, password=password)
            data["password_storage"] = PASSWORD_STORAGE_KEYRING
            data.pop("password", None)
        elif storage == PASSWORD_STORAGE_PLAIN:
            data["password_storage"] = PASSWORD_STORAGE_PLAIN
            data["password"] = password
        else:
            raise ConfigError(f"未知 password_storage: {password_storage}")
    _write(data)


def save_credentials(*, username: str, password: str) -> None:
    """Persist SSO credentials so the daemon can auto-refresh tokens without env vars."""
    data = _load_file()
    data["username"] = username.strip()
    _save_password_keyring(username=username, password=password)
    data["password_storage"] = PASSWORD_STORAGE_KEYRING
    data.pop("password", None)
    _write(data)


def clear_auth() -> None:
    try:
        data = _load_file()
        if data.get("password_storage") == PASSWORD_STORAGE_KEYRING and data.get("username"):
            _delete_password_keyring(username=str(data["username"]))
    except ConfigError:
        pass
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()


def _pick(key: str, *, file_data: dict, env_file: dict, env_key: str | None = None) -> str:
    """Resolution order: real env var > .env file > config file."""
    real_key = env_key or f"BHLIB_{key.upper()}"
    return (
        os.environ.get(real_key)
        or env_file.get(real_key)
        or str(file_data.get(key) or "")
    ).strip()


def load_auth() -> AuthConfig:
    auth = load_auth_loose()
    if not auth.token:
        raise ConfigError("缺少 token：请先运行 `bhlib login`")
    if not auth.cookie:
        raise ConfigError("缺少 cookie：请先运行 `bhlib login`")
    return auth


def load_auth_loose() -> AuthConfig:
    """Like load_auth() but tolerates missing token/cookie (for bootstrap)."""
    file_data = _load_file()
    env_file = load_env()

    token = _pick("token", file_data=file_data, env_file=env_file)
    cookie = _pick("cookie", file_data=file_data, env_file=env_file)
    base_url = _pick("base_url", file_data=file_data, env_file=env_file) or DEFAULT_BASE_URL

    env_insecure = _pick("insecure", file_data={}, env_file=env_file)
    verify_ssl = False if env_insecure else bool(file_data.get("verify_ssl", True))

    default_area_id = _pick("default_area_id", file_data=file_data, env_file=env_file) or None
    seat_format = (
        os.environ.get("BHLIB_SEAT_FORMAT")
        or env_file.get("BHLIB_SEAT_FORMAT")
        or str(file_data.get("seat_format") or "").strip()
        or None
    )
    username = _pick("username", file_data=file_data, env_file=env_file) or None
    password_storage = str(file_data.get("password_storage") or "").strip().lower() or None
    password = os.environ.get("BHLIB_PASSWORD") or env_file.get("BHLIB_PASSWORD") or ""
    if not password:
        if password_storage == PASSWORD_STORAGE_KEYRING and username:
            password = _load_password_keyring(username=username) or ""
        else:
            # Backward-compatible read path for old configs and explicit plain fallback.
            password = file_data.get("password") or ""
            if password and not password_storage:
                password_storage = PASSWORD_STORAGE_PLAIN
    password = password or None

    return AuthConfig(
        token=token,
        cookie=cookie,
        base_url=base_url,
        verify_ssl=verify_ssl,
        default_area_id=default_area_id,
        seat_format=seat_format,
        username=username,
        password=password,
        password_storage=password_storage,
    )


def update_defaults(
    *, default_area_id: str | None = None, seat_format: str | None = None
) -> None:
    """Update defaults without touching token/cookie."""
    data = _load_file()
    if default_area_id is not None:
        data["default_area_id"] = str(default_area_id).strip() or None
    if seat_format is not None:
        fmt = str(seat_format).strip().lower()
        if fmt not in ("map", "list", ""):
            raise ConfigError(f"seat_format 必须是 map 或 list，收到: {seat_format}")
        data["seat_format"] = fmt or None
    if not data:
        raise ConfigError("配置为空：请先运行 `bhlib login`")
    _write(data)


def get_cached_area_tree(*, max_age_sec: int = 86400) -> dict | None:
    import time as _t
    data = _load_file()
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
    import time as _t
    data = _load_file()
    data["area_tree_cache"] = {"fetched_at": int(_t.time()), "tree": tree}
    _write(data)


def get_cached_segment(*, area_id: str, start_time: str, end_time: str) -> str | None:
    data = _load_file()
    cache = data.get("segment_cache")
    if not isinstance(cache, dict):
        return None
    key = f"{str(area_id).strip()}|{str(start_time).strip()}|{str(end_time).strip()}"
    seg = cache.get(key)
    return str(seg).strip() if seg is not None and str(seg).strip() else None


def cache_segment(*, area_id: str, start_time: str, end_time: str, segment: str) -> None:
    segment = str(segment).strip()
    if not segment:
        return
    data = _load_file()
    cache = data.get("segment_cache")
    if not isinstance(cache, dict):
        cache = {}
    key = f"{str(area_id).strip()}|{str(start_time).strip()}|{str(end_time).strip()}"
    cache[key] = segment
    data["segment_cache"] = cache
    _write(data)


def save_pomo_state(state: dict) -> None:
    data = _load_file()
    data["pomo_daemon"] = state
    _write(data)


def load_pomo_state() -> dict | None:
    data = _load_file()
    state = data.get("pomo_daemon")
    if isinstance(state, dict):
        return state
    return None


def clear_pomo_state() -> None:
    data = _load_file()
    if "pomo_daemon" in data:
        del data["pomo_daemon"]
        _write(data)


def is_pomo_running() -> bool:
    """Check if pomodoro daemon is running based on saved PID."""
    import sys

    state = load_pomo_state()
    if not isinstance(state, dict):
        return False

    pid = state.get("pid")
    if not isinstance(pid, int):
        return False

    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, AttributeError):
        return False
