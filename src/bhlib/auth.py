from __future__ import annotations

import base64
import datetime as _dt
import json

from .cas import cas_login
from .config import ConfigError, load_auth_loose, save_auth
from .env import load_env


def _b64url_decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    pad = "=" * ((4 - (len(data) % 4)) % 4)
    return base64.b64decode(data + pad)


def decode_jwt_payload(token: str) -> dict:
    """
    Decode JWT payload without verifying signature.
    """
    token = (token or "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        raise ConfigError("token 不是合法 JWT")
    try:
        raw = _b64url_decode(parts[1]).decode("utf-8", errors="replace")
        obj = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise ConfigError("无法解析 JWT payload") from e
    if not isinstance(obj, dict):
        raise ConfigError("JWT payload 结构异常")
    return obj


def _parse_hhmm(s: str) -> tuple[int, int]:
    s = (s or "").strip()
    if not s:
        return (18, 5)
    if ":" not in s:
        raise ConfigError("BHLIB_TOKEN_REFRESH_AT 必须形如 HH:MM")
    hh, mm = s.split(":", 1)
    if not (hh.isdigit() and mm.isdigit()):
        raise ConfigError("BHLIB_TOKEN_REFRESH_AT 必须形如 HH:MM")
    h = int(hh)
    m = int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError("BHLIB_TOKEN_REFRESH_AT 超出范围")
    return (h, m)


def should_refresh_token(token: str) -> bool:
    """
    Refresh policy:
    - If exp is near/expired => refresh.
    - If local time >= BHLIB_TOKEN_REFRESH_AT (default 18:05) and token iat date < today => refresh.
    """
    payload = decode_jwt_payload(token)
    now = _dt.datetime.now()
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        # refresh if expires within 5 minutes
        if now.timestamp() >= float(exp) - 300:
            return True

    env = load_env()
    hh, mm = _parse_hhmm(env.get("BHLIB_TOKEN_REFRESH_AT", "18:05") or "18:05")
    refresh_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < refresh_time:
        return False

    iat = payload.get("iat")
    if isinstance(iat, (int, float)):
        iat_dt = _dt.datetime.fromtimestamp(float(iat))
        # If token was issued before today's refresh window, refresh.
        if iat_dt < refresh_time:
            return True
    return False


def ensure_logged_in(
    *,
    insecure: bool = False,
    timeout_sec: float = 20.0,
    force: bool = False,
    use_proxy: bool | None = None,
) -> None:
    """
    Ensure we have a usable token/cookie. If missing or policy says refresh, do CAS login via .env credentials.
    """
    auth = load_auth_loose()
    if auth.token:
        try:
            if (not force) and (not should_refresh_token(auth.token)):
                return
        except ConfigError:
            # if token can't be decoded, just refresh
            pass

    username = (auth.username or "").strip()
    password = auth.password or ""
    if not username or not password:
        raise ConfigError(
            "需要自动刷新 token 但缺少凭证：请运行 `bhlib login` 重新登录"
            "（会把账号密码存到 ~/.bhlib/config.json）"
        )

    result = cas_login(
        username=username,
        password=password,
        initial_booking_cookie=auth.cookie or None,
        timeout_sec=timeout_sec,
        verify_ssl=(not insecure) and auth.verify_ssl,
        use_proxy=bool(use_proxy),
    )
    save_auth(
        token=result.token,
        cookie=result.cookie,
        base_url=auth.base_url,
        verify_ssl=(not insecure) and auth.verify_ssl,
        username=username,
        password=password,
    )
