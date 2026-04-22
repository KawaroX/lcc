from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from getpass import getpass

from .api import post_json_authed
from .areas import flatten_areas, get_or_fetch_tree, resolve_area_id
from .config import (
    CONFIG_FILE,
    ConfigError,
    clear_auth,
    cache_segment,
    get_cached_segment,
    load_auth,
    load_auth_loose,
    save_auth,
    update_defaults,
)
from .cas import CasLoginError, cas_login
from .crypto import CryptoError, aesjson_decrypt, aesjson_encrypt
from .http import HttpError
from .env import load_env
from .seatmap import render_seat_map


def _normalize_day_yyyy_mm_dd(v: str) -> str:
    s = str(v or "").strip()
    if not s:
        raise ConfigError("日期不能为空（应为 YYYY-MM-DD，例如 2026-04-21）")
    if s.isdigit() and len(s) == 8:
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        _dt.date.fromisoformat(s)
    except ValueError as e:
        raise ConfigError(f"日期格式错误：{v}（应为 YYYY-MM-DD，例如 2026-04-21）") from e
    return s


def _normalize_time_hh_mm(v: str, *, flag: str) -> str:
    s = str(v or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        raise ConfigError(f"{flag} 时间格式错误：{v}（应为 HH:MM，例如 07:00）")
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ConfigError(f"{flag} 时间超出范围：{v}（应为 00:00-23:59）")
    return f"{hh:02d}:{mm:02d}"


def _time_hh_mm_to_minutes(v: str) -> int:
    # v must already be normalized HH:MM.
    hh, mm = v.split(":", 1)
    return int(hh) * 60 + int(mm)


def _effective_verify_ssl(auth, args: argparse.Namespace) -> bool:
    if getattr(args, "insecure", False):
        return False
    return bool(getattr(auth, "verify_ssl", True))

def _effective_use_proxy(args: argparse.Namespace) -> bool:
    # Default: no proxy (campus network). Opt-in via --proxy or BHLIB_PROXY=1.
    if getattr(args, "proxy", False):
        return True
    v = (os.environ.get("BHLIB_PROXY") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    return False


def _interactive_pick_area(args: argparse.Namespace, auth) -> str:
    """Show a flat list of areas with free/total and ask the user to pick one.
    Returns the chosen area_id as a string. Raises ConfigError on cancel.
    """
    verify_ssl = _effective_verify_ssl(auth, args)
    tree = get_or_fetch_tree(
        timeout_sec=float(getattr(args, "timeout", 15.0)),
        insecure=bool(getattr(args, "insecure", False)),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    items = flatten_areas(tree)
    if not items:
        raise ConfigError("获取区域列表为空")

    print("未设置默认区域，请选择：")
    for idx, a in enumerate(items, 1):
        label = f"{a.get('premiseName', '')} / {a.get('storeyName', '')} / {a.get('name', '')}".strip(" /")
        free = a.get("freeNum")
        total = a.get("totalNum")
        suffix = f"  [{free}/{total}]" if free is not None and total is not None else ""
        print(f"  {idx:>3}. {label}{suffix}  (id={a.get('id')})")
    try:
        raw = input("序号 / 名字 / id: ").strip()
    except EOFError:
        raise ConfigError("未选择区域") from None
    if not raw:
        raise ConfigError("未选择区域")

    # numeric index into the list
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(items):
            return str(items[n - 1]["id"])
        # else treat as area id

    return str(resolve_area_id(raw, tree=tree))


def _parse_light_arg(v: str) -> int:
    """on → 20, off → 0, otherwise int in [0, 100]."""
    s = str(v or "").strip().lower()
    if s == "on":
        return 20
    if s == "off":
        return 0
    try:
        n = int(s)
    except ValueError as e:
        raise ConfigError(f"无法识别的亮度：{v}（允许 on / off / 0-100）") from e
    if not 0 <= n <= 100:
        raise ConfigError(f"亮度超出范围：{n}（应在 0-100）")
    return n


def _parse_duration_to_seconds(v: str) -> float:
    """
    '25' → 25 min, '25m' → 25 min, '1h' → 60 min.
    Bare s/d/other suffixes are rejected (s too short, d too long for one session).
    """
    s = str(v or "").strip().lower()
    if not s:
        raise ConfigError("时长不能为空")
    if s.endswith("h"):
        try:
            return float(s[:-1]) * 3600.0
        except ValueError as e:
            raise ConfigError(f"无法识别的时长：{v}") from e
    if s.endswith("m"):
        try:
            return float(s[:-1]) * 60.0
        except ValueError as e:
            raise ConfigError(f"无法识别的时长：{v}") from e
    # Any other trailing letter is rejected.
    if s[-1].isalpha():
        raise ConfigError(f"时长单位只支持 m 或 h：{v}")
    try:
        return float(s) * 60.0  # plain number = minutes
    except ValueError as e:
        raise ConfigError(f"无法识别的时长：{v}") from e

def _fetch_subscribe(args: argparse.Namespace, auth, *, timeout: float, verify_ssl: bool, insecure: bool) -> object:
    return post_json_authed(
        path="/v4/index/subscribe",
        json_body={},
        timeout_sec=timeout,
        insecure=bool(insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )


def _pick_my_light_device(subscribe_resp: object, *, prefer_area_id: str | None = None) -> dict:
    if not isinstance(subscribe_resp, dict):
        raise ConfigError("subscribe 返回结构异常：不是对象")
    data = subscribe_resp.get("data")
    if not isinstance(data, list):
        raise ConfigError("subscribe 返回结构异常：data 不是数组")

    prefer_area_id = str(prefer_area_id).strip() if prefer_area_id is not None else None

    def _has_light(it: dict) -> bool:
        v = it.get("hasLight")
        return v in (1, "1", True)

    candidates: list[dict] = [it for it in data if isinstance(it, dict) and _has_light(it)]
    if prefer_area_id:
        preferred = [it for it in candidates if str(it.get("area_id") or "") == prefer_area_id]
        if preferred:
            candidates = preferred

    if not candidates:
        raise ConfigError("未找到可用的灯光设备（subscribe 里没有 hasLight=1 的条目）")

    picked = candidates[0]
    if not picked.get("id") or not picked.get("area_id"):
        raise ConfigError("subscribe 条目缺少 id/area_id")
    return picked


def _pick_my_active_item(subscribe_resp: object, *, prefer_area_id: str | None = None) -> dict:
    if not isinstance(subscribe_resp, dict):
        raise ConfigError("subscribe 返回结构异常：不是对象")
    data = subscribe_resp.get("data")
    if not isinstance(data, list) or not data:
        raise ConfigError("subscribe 里没有数据（可能当前没有座位/预约）")

    prefer_area_id = str(prefer_area_id).strip() if prefer_area_id is not None else None
    items = [it for it in data if isinstance(it, dict)]
    if prefer_area_id:
        preferred = [it for it in items if str(it.get("area_id") or "") == prefer_area_id]
        if preferred:
            items = preferred
    return items[0]


def _space_payload_from_subscribe_item(item: dict, *, style: str) -> dict:
    device_id = str(item.get("id") or "").strip()
    seat_id = str(item.get("space_id") or item.get("space") or "").strip()
    area_id = str(item.get("area_id") or "").strip()

    if style == "device_points":
        if not device_id:
            raise ConfigError("subscribe 条目里没有找到 id（smartDevice id）")
        return {"id": device_id, "points": {}}
    if style == "id":
        if not seat_id:
            raise ConfigError("subscribe 条目里没有找到 space_id/space")
        if not area_id:
            raise ConfigError("subscribe 条目里没有找到 area_id")
        return {"id": seat_id, "area_id": area_id}
    if style == "space_id":
        if not seat_id:
            raise ConfigError("subscribe 条目里没有找到 space_id/space")
        if not area_id:
            raise ConfigError("subscribe 条目里没有找到 area_id")
        return {"space_id": seat_id, "area_id": area_id}
    raise ConfigError(f"未知 style: {style}")


def _fetch_seat_resp(
    args: argparse.Namespace,
    *,
    area_id: str,
    day: str,
    start_time: str,
    end_time: str,
    timeout: float,
    insecure: bool,
    verify_ssl: bool,
) -> dict:
    resp = post_json_authed(
        path="/v4/Space/seat",
        json_body={
            "id": str(area_id),
            "day": day,
            "label_id": [],
            "start_time": start_time,
            "end_time": end_time,
            "begdate": "",
            "enddate": "",
        },
        timeout_sec=timeout,
        insecure=insecure,
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    if not isinstance(resp, dict):
        raise ConfigError("seat 接口返回结构异常：不是对象")
    return resp


def _extract_segment_from_seat_resp(resp: dict) -> str | None:
    d = resp.get("data")
    if isinstance(d, dict):
        for k in ("segment", "segment_id", "segmentId"):
            v = d.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return None


def _discover_segment_in_obj(obj: object, *, start_time: str, end_time: str) -> str | None:
    """
    Best-effort segment discovery from an arbitrary JSON object.
    Looks for dicts that contain a segment id, optionally matching start/end time.
    """

    def _iter_dicts(x: object):
        stack = [x]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                yield cur
                for v in cur.values():
                    stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)

    start_time = (start_time or "").strip()
    end_time = (end_time or "").strip()
    candidates: list[tuple[str, str | None, str | None]] = []
    for d in _iter_dicts(obj):
        seg = d.get("segment") or d.get("segment_id") or d.get("segmentId")
        if seg is None:
            continue
        seg_s = str(seg).strip()
        if not seg_s:
            continue
        st = d.get("start_time") or d.get("startTime") or d.get("beginTime") or d.get("begin_time")
        et = d.get("end_time") or d.get("endTime")
        st_s = str(st).strip() if st is not None else None
        et_s = str(et).strip() if et is not None else None
        candidates.append((seg_s, st_s, et_s))

    if not candidates:
        return None

    # Prefer exact time match when possible.
    for seg_s, st_s, et_s in candidates:
        if st_s and et_s and st_s == start_time and et_s == end_time:
            return seg_s

    # If there is only one unique segment in the object, use it.
    uniq = sorted({c[0] for c in candidates})
    if len(uniq) == 1:
        return uniq[0]

    return None


def _fetch_segment_from_map(
    args: argparse.Namespace,
    *,
    area_id: str,
    day: str,
    start_time: str,
    end_time: str,
    verify_ssl: bool,
) -> str | None:
    """
    Fetch segment from /v4/Space/map.
    Response: data.date.list[*].times[*].{id, start, end}
    where times[i].id IS the segment value for that time slot.
    """
    try:
        resp = post_json_authed(
            path="/v4/Space/map",
            json_body={"id": area_id},
            timeout_sec=float(getattr(args, "timeout", 15)),
            insecure=bool(getattr(args, "insecure", False)),
            verify_ssl=verify_ssl,
            use_proxy=_effective_use_proxy(args),
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    if not isinstance(data, dict):
        return None
    date_obj = data.get("date")
    if not isinstance(date_obj, dict):
        return None
    date_list = date_obj.get("list")
    if not isinstance(date_list, list):
        return None

    # Prefer the entry for the requested day; fall back to all entries.
    day_entries = [e for e in date_list if isinstance(e, dict) and str(e.get("day", "")).startswith(day)]
    if not day_entries:
        day_entries = [e for e in date_list if isinstance(e, dict)]

    start_time = (start_time or "").strip()
    end_time = (end_time or "").strip()
    candidates: list[tuple[str, str, str]] = []  # (seg_id, t_start, t_end)
    for entry in day_entries:
        times = entry.get("times")
        if not isinstance(times, list):
            continue
        for t in times:
            if not isinstance(t, dict):
                continue
            seg_id = str(t.get("id") or "").strip()
            if not seg_id:
                continue
            t_start = str(t.get("start") or "").strip()
            t_end = str(t.get("end") or "").strip()
            candidates.append((seg_id, t_start, t_end))

    if not candidates:
        return None
    # Exact match first.
    for seg_id, t_start, t_end in candidates:
        if t_start == start_time and t_end == end_time:
            return seg_id
    # Time slot that contains the requested range.
    for seg_id, t_start, t_end in candidates:
        if t_start and t_end and t_start <= start_time and t_end >= end_time:
            return seg_id
    # Single candidate — use it.
    if len(candidates) == 1:
        return candidates[0][0]
    return None


def _extract_segment_from_list_resp(resp: object, *, start_time: str, end_time: str) -> str | None:
    """
    Extract segment from a segment-list style response where each item's own
    'id' (or similar) field IS the segment value, paired with start/end time fields.

    Example response shape:
      {"data": [{"id": "2285237", "start_time": "19:00", "end_time": "23:00"}, ...]}
    or
      {"data": {"list": [{"id": "2285237", "startTime": "19:00", "endTime": "23:00"}, ...]}}
    """
    def _iter_items(x: object):
        if isinstance(x, dict):
            data = x.get("data")
            if isinstance(data, list):
                yield from (i for i in data if isinstance(i, dict))
                return
            if isinstance(data, dict):
                for key in ("list", "rows", "items", "segments", "times"):
                    lst = data.get(key)
                    if isinstance(lst, list):
                        yield from (i for i in lst if isinstance(i, dict))
                        return
                # data itself might be the only item
                yield from _iter_items(data)
        elif isinstance(x, list):
            yield from (i for i in x if isinstance(i, dict))

    start_time = (start_time or "").strip()
    end_time = (end_time or "").strip()
    candidates: list[tuple[str, str | None, str | None]] = []

    for item in _iter_items(resp):
        # The segment value is the item's own id.
        seg = item.get("id") or item.get("segmentId") or item.get("segment_id")
        if seg is None:
            continue
        seg_s = str(seg).strip()
        if not seg_s:
            continue
        st = (item.get("start_time") or item.get("startTime")
              or item.get("beginTime") or item.get("begin_time"))
        et = item.get("end_time") or item.get("endTime")
        st_s = str(st).strip() if st is not None else None
        et_s = str(et).strip() if et is not None else None
        candidates.append((seg_s, st_s, et_s))

    if not candidates:
        return None

    # Prefer exact time-range match.
    for seg_s, st_s, et_s in candidates:
        if st_s and et_s and st_s == start_time and et_s == end_time:
            return seg_s

    # If only one candidate, use it.
    if len(candidates) == 1:
        return candidates[0][0]

    return None


def _fetch_segment_from_api(
    args: argparse.Namespace,
    *,
    area_id: str,
    day: str,
    start_time: str,
    end_time: str,
    verify_ssl: bool,
) -> str | None:
    """
    Try known segment-list endpoints to auto-discover the segment ID for
    (area_id, day, start_time, end_time).  Tries both segment-list format
    (each item's id IS the segment) and embedded-segment format.
    """
    candidate_paths = [
        "/v4/Space/segment",
        "/v4/space/segment",
        "/v4/area/segment",
        "/v4/Space/time",
        "/v4/area/time",
        "/v4/Space/opendays",
    ]
    payloads = [
        {"id": area_id, "day": day},
        {"area_id": area_id, "day": day},
        {"id": area_id},
        {"area_id": area_id},
    ]
    for path in candidate_paths:
        for payload in payloads:
            try:
                resp = post_json_authed(
                    path=path,
                    json_body=payload,
                    timeout_sec=float(getattr(args, "timeout", 15)),
                    insecure=bool(getattr(args, "insecure", False)),
                    verify_ssl=verify_ssl,
                    use_proxy=_effective_use_proxy(args),
                )
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(resp, dict):
                continue
            # Try both extraction strategies.
            seg = _extract_segment_from_list_resp(
                resp, start_time=start_time, end_time=end_time
            ) or _discover_segment_in_obj(resp, start_time=start_time, end_time=end_time)
            if seg:
                return seg
    return None


def _extract_seats_from_seat_resp(resp: dict) -> list[dict]:
    d = resp.get("data")
    if not isinstance(d, dict):
        return []
    lst = d.get("list")
    if not isinstance(lst, list):
        return []
    return [it for it in lst if isinstance(it, dict)]


def _cmd_auth_set(args: argparse.Namespace) -> int:
    # Preserve defaults if file exists
    default_area_id = None
    try:
        default_area_id = load_auth().default_area_id
    except ConfigError:
        default_area_id = None
    save_auth(
        token=args.token,
        cookie=args.cookie,
        base_url=args.base_url,
        verify_ssl=(not args.insecure),
        default_area_id=default_area_id,
    )
    print(f"OK: 已写入 {CONFIG_FILE}")
    return 0


def _redact(value: str, keep: int = 6) -> str:
    value = value or ""
    if len(value) <= keep:
        return "*" * len(value)
    return ("*" * (len(value) - keep)) + value[-keep:]


def _print_api_result(data: object) -> None:
    """Pretty-print a typical API response: show a checkmark + message on success,
    otherwise preserve the full JSON."""
    if not isinstance(data, dict):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    code = data.get("code")
    msg = data.get("message") or data.get("msg")
    if code == 0 and msg:
        print(f"✅ {msg}")
        extra = data.get("data")
        if extra is not None and extra != [] and extra != {}:
            print(json.dumps(extra, ensure_ascii=False, indent=2))
    elif code is not None and code != 0 and msg:
        print(f"❌ [{code}] {msg}")
        extra = data.get("data")
        if extra is not None and extra != [] and extra != {}:
            print(json.dumps(extra, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _cmd_auth_show(args: argparse.Namespace) -> int:
    auth = load_auth()
    print(json.dumps(
        {
            "base_url": auth.base_url,
            "token": _redact(auth.token),
            "cookie": _redact(auth.cookie),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def _cmd_auth_clear(args: argparse.Namespace) -> int:
    clear_auth()
    print(f"OK: 已删除 {CONFIG_FILE}")
    return 0


def _cmd_auth_login(args: argparse.Namespace) -> int:
    env = load_env()

    username = (
        (args.username or "").strip()
        or (env.get("BHLIB_USERNAME") or "").strip()
        or (load_auth_loose().username or "").strip()
    )
    if not username:
        if args.no_prompt:
            raise ConfigError("缺少 username：请传 --username 或设置 BHLIB_USERNAME")
        username = input("学号: ").strip()
        if not username:
            raise ConfigError("学号不能为空")

    password = (
        args.password
        or (env.get("BHLIB_PASSWORD") or "")
    )
    if not password:
        if args.no_prompt:
            raise ConfigError("缺少密码：请传 --password 或设置 BHLIB_PASSWORD")
        password = getpass("密码: ")
        if not password:
            raise ConfigError("密码不能为空")

    try:
        result = cas_login(
            username=username,
            password=password,
            initial_booking_cookie=args.seed_cookie,
            timeout_sec=args.timeout,
            verify_ssl=(not args.insecure),
        )
    except CasLoginError as e:
        raise ConfigError(str(e)) from e

    default_area_id = None
    try:
        default_area_id = load_auth_loose().default_area_id
    except ConfigError:
        default_area_id = None
    save_auth(
        token=result.token,
        cookie=result.cookie,
        base_url=args.base_url,
        verify_ssl=(not args.insecure),
        default_area_id=default_area_id,
        username=username,
        password=password,
    )
    print(f"OK: 登录成功（{username}），配置已写入 {CONFIG_FILE}")
    return 0


def _cmd_light_set(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    sub = _fetch_subscribe(args, auth, timeout=float(args.timeout), verify_ssl=verify_ssl, insecure=bool(args.insecure))

    device_id_arg = str(args.device_id).strip() if getattr(args, "device_id", None) is not None else None
    area_id_arg = str(args.area_id).strip() if getattr(args, "area_id", None) is not None else None

    if device_id_arg:
        picked = None
        if isinstance(sub, dict) and isinstance(sub.get("data"), list):
            for it in sub["data"]:
                if not isinstance(it, dict):
                    continue
                if str(it.get("id") or "").strip() != device_id_arg:
                    continue
                if it.get("hasLight") not in (1, "1", True):
                    continue
                if area_id_arg and str(it.get("area_id") or "").strip() != area_id_arg:
                    continue
                picked = it
                break
        if not picked:
            raise ConfigError(
                "指定的 --device-id/--area-id 不在当前账号的 subscribe(hasLight=1) 列表里；"
                "出于安全考虑，不支持控制非本人座位的灯。"
            )
    else:
        picked = _pick_my_light_device(sub, prefer_area_id=args.prefer_area_id)

    device_id = str(picked["id"])
    area_id = str(picked["area_id"])

    payload = {
        "id": device_id,
        "area_id": area_id,
        "brightness": int(args.brightness),
    }
    data = post_json_authed(
        path="/reserve/smartDevice/setLightBrightness",
        json_body=payload,
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    _print_api_result(data)
    return 0


def _resolve_my_light_device_ids(
    args: argparse.Namespace,
    *,
    timeout: float,
) -> tuple[str, str, bool]:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    sub = _fetch_subscribe(args, auth, timeout=timeout, verify_ssl=verify_ssl, insecure=bool(args.insecure))
    picked = _pick_my_light_device(sub, prefer_area_id=getattr(args, "prefer_area_id", None))
    return str(picked["id"]), str(picked["area_id"]), verify_ssl


def _set_light_brightness_by_ids(
    *,
    device_id: str,
    area_id: str,
    brightness: int,
    timeout: float,
    insecure: bool,
    verify_ssl: bool,
    use_proxy: bool,
) -> object:
    payload = {"id": str(device_id), "area_id": str(area_id), "brightness": int(brightness)}
    return post_json_authed(
        path="/reserve/smartDevice/setLightBrightness",
        json_body=payload,
        timeout_sec=float(timeout),
        insecure=bool(insecure),
        verify_ssl=bool(verify_ssl),
        use_proxy=bool(use_proxy),
    )


def _flash_light_brightness(
    args: argparse.Namespace,
    *,
    low: int,
    high: int,
    cycles: int,
    interval: float,
) -> None:
    if cycles <= 0:
        raise ConfigError("--cycles 必须是正整数")
    if interval < 0:
        raise ConfigError("--interval 不能为负数")

    timeout = float(getattr(args, "timeout", 15.0))
    device_id, area_id, verify_ssl = _resolve_my_light_device_ids(args, timeout=timeout)

    # Pattern: low -> (high -> low) * cycles  (so high is reached `cycles` times)
    _set_light_brightness_by_ids(
        device_id=device_id,
        area_id=area_id,
        brightness=int(low),
        timeout=timeout,
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    if interval > 0:
        time.sleep(float(interval))
    for _ in range(int(cycles)):
        _set_light_brightness_by_ids(
            device_id=device_id,
            area_id=area_id,
            brightness=int(high),
            timeout=timeout,
            insecure=bool(args.insecure),
            verify_ssl=verify_ssl,
            use_proxy=_effective_use_proxy(args),
        )
        if interval > 0:
            time.sleep(float(interval))
        _set_light_brightness_by_ids(
            device_id=device_id,
            area_id=area_id,
            brightness=int(low),
            timeout=timeout,
            insecure=bool(args.insecure),
            verify_ssl=verify_ssl,
            use_proxy=_effective_use_proxy(args),
        )
        if interval > 0:
            time.sleep(float(interval))


def _cmd_pomo_flash(args: argparse.Namespace) -> int:
    _flash_light_brightness(
        args,
        low=int(args.low),
        high=int(args.high),
        cycles=int(args.cycles),
        interval=float(args.interval),
    )
    print(f"OK: 已闪烁 {args.cycles} 次（{args.low}->{args.high}->{args.low}）")
    return 0


def _cmd_pomo_start(args: argparse.Namespace) -> int:
    if args.seconds is not None:
        total_sec = float(args.seconds)
    else:
        total_sec = float(args.minutes) * 60.0
    if total_sec <= 0:
        raise ConfigError("番茄钟时长必须为正数（duration，如 25 / 25m / 1h）")

    end_at = _dt.datetime.now() + _dt.timedelta(seconds=total_sec)
    print(f"Pomodoro 开始：{total_sec:.0f}s，预计结束时间：{end_at.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        time.sleep(total_sec)
    except KeyboardInterrupt:
        print("已取消（Ctrl-C）")
        return 130

    print("时间到：开始闪烁灯光…")
    _flash_light_brightness(
        args,
        low=int(args.low),
        high=int(args.high),
        cycles=int(args.cycles),
        interval=float(args.interval),
    )
    print("OK: 番茄钟完成")
    return 0


def _cmd_me(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    data = _fetch_subscribe(args, auth, timeout=float(args.timeout), verify_ssl=verify_ssl, insecure=bool(args.insecure))

    if getattr(args, "raw", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    try:
        item = _pick_my_active_item(data, prefer_area_id=getattr(args, "prefer_area_id", None))
    except ConfigError:
        print(json.dumps({"active": False}, ensure_ascii=False, indent=2))
        return 0

    out = {
        "area_id": item.get("area_id"),
        "seat_no": item.get("no") or item.get("spaceName") or "",
        "status": item.get("statusname") or item.get("status_name") or "",
        "brightness": item.get("brightness"),
        "device_id": item.get("id"),
        "area": item.get("areaName") or item.get("nameMerge") or "",
        "beginTime": item.get("beginTime"),
        "endTime": item.get("endTime"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_crypto_encrypt(args: argparse.Namespace) -> int:
    try:
        data = json.loads(args.data)
    except json.JSONDecodeError as e:
        raise ConfigError(f"--data 不是合法 JSON: {e}") from e
    try:
        s = aesjson_encrypt(data, day=args.day)
    except CryptoError as e:
        raise ConfigError(str(e)) from e
    print(s)
    return 0


def _cmd_crypto_decrypt(args: argparse.Namespace) -> int:
    try:
        s = aesjson_decrypt(args.aesjson, day=args.day)
    except CryptoError as e:
        raise ConfigError(str(e)) from e

    s_strip = s.strip()
    if args.json:
        try:
            obj = json.loads(s_strip)
        except json.JSONDecodeError:
            obj = {"plaintext": s_strip}
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(s_strip)
    return 0


def _cmd_space_leave(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    if args.data:
        try:
            payload = json.loads(args.data)
        except json.JSONDecodeError as e:
            raise ConfigError(f"--data 不是合法 JSON: {e}") from e
    else:
        sub = _fetch_subscribe(args, auth, timeout=float(args.timeout), verify_ssl=verify_ssl, insecure=bool(args.insecure))
        item = _pick_my_active_item(sub, prefer_area_id=args.prefer_area_id)
        payload = _space_payload_from_subscribe_item(item, style=args.style)

    aesjson = aesjson_encrypt(payload, day=args.day)
    if args.dry_run:
        print(json.dumps({"payload": payload, "aesjson": aesjson}, ensure_ascii=False, indent=2))
        return 0

    data = post_json_authed(
        path="/v4/space/leave",
        json_body={"aesjson": aesjson},
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    _print_api_result(data)
    return 0


def _cmd_space_signin(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    if args.data:
        try:
            payload = json.loads(args.data)
        except json.JSONDecodeError as e:
            raise ConfigError(f"--data 不是合法 JSON: {e}") from e
    else:
        sub = _fetch_subscribe(args, auth, timeout=float(args.timeout), verify_ssl=verify_ssl, insecure=bool(args.insecure))
        item = _pick_my_active_item(sub, prefer_area_id=args.prefer_area_id)
        payload = _space_payload_from_subscribe_item(item, style=args.style)

    aesjson = aesjson_encrypt(payload, day=args.day)
    if args.dry_run:
        print(json.dumps({"payload": payload, "aesjson": aesjson}, ensure_ascii=False, indent=2))
        return 0

    data = post_json_authed(
        path="/v4/space/signin",
        json_body={"aesjson": aesjson},
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    _print_api_result(data)
    return 0


def _cmd_space_action(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)

    path = str(args.path or "").strip()
    if not path:
        raise ConfigError("缺少 --path（例如 /v4/space/leave）")

    if args.data:
        try:
            payload = json.loads(args.data)
        except json.JSONDecodeError as e:
            raise ConfigError(f"--data 不是合法 JSON: {e}") from e
    else:
        sub = _fetch_subscribe(args, auth, timeout=float(args.timeout), verify_ssl=verify_ssl, insecure=bool(args.insecure))
        item = _pick_my_active_item(sub, prefer_area_id=args.prefer_area_id)
        payload = _space_payload_from_subscribe_item(item, style=args.style)

    aesjson = aesjson_encrypt(payload, day=args.day)
    if args.dry_run:
        print(json.dumps({"path": path, "payload": payload, "aesjson": aesjson}, ensure_ascii=False, indent=2))
        return 0

    data = post_json_authed(
        path=path,
        json_body={"aesjson": aesjson},
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    _print_api_result(data)
    return 0


def _cmd_space_finish(args: argparse.Namespace) -> int:
    args.path = "/v4/space/checkout"
    return _cmd_space_action(args)


def _cmd_space_book(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    env = load_env()

    day = _normalize_day_yyyy_mm_dd(args.day) if args.day else _dt.date.today().isoformat()
    start_time = _normalize_time_hh_mm(
        (args.start_time or _dt.datetime.now().strftime("%H:%M")),
        flag="--start",
    )
    end_time = _normalize_time_hh_mm((args.end_time or "23:00"), flag="--end")

    area_id = _resolve_area_id_maybe(args.area_id, args, auth=auth) or auth.default_area_id
    if not area_id:
        area_id = _interactive_pick_area(args, auth)

    if _time_hh_mm_to_minutes(start_time) >= _time_hh_mm_to_minutes(end_time):
        raise ConfigError(
            f"时间区间无效：start_time={start_time} end_time={end_time}（请检查 --start/--end；格式 HH:MM，例如 --start 07:00 --end 23:00；日期用 --day YYYY-MM-DD）"
        )

    # Fetch seat list (for both display and segment discovery).
    seat_resp = _fetch_seat_resp(
        args,
        area_id=str(area_id),
        day=day,
        start_time=start_time,
        end_time=end_time,
        timeout=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
    )
    segment = (str(args.segment).strip() if args.segment else None) \
        or _extract_segment_from_seat_resp(seat_resp) \
        or _discover_segment_in_obj(seat_resp, start_time=start_time, end_time=end_time)
    if not segment:
        # Try another seat call with empty time range: some deployments return segment lists that way.
        try:
            seat_resp2 = _fetch_seat_resp(
                args,
                area_id=str(area_id),
                day=day,
                start_time="",
                end_time="",
                timeout=float(args.timeout),
                insecure=bool(args.insecure),
                verify_ssl=verify_ssl,
            )
        except Exception:  # noqa: BLE001
            seat_resp2 = {}
        segment = _extract_segment_from_seat_resp(seat_resp2) or _discover_segment_in_obj(
            seat_resp2, start_time=start_time, end_time=end_time
        )
    if not segment:
        segment = _fetch_segment_from_map(
            args,
            area_id=str(area_id),
            day=day,
            start_time=start_time,
            end_time=end_time,
            verify_ssl=verify_ssl,
        )
    if not segment:
        # Try the dedicated segment endpoint that some deployments expose.
        segment = _fetch_segment_from_api(
            args,
            area_id=str(area_id),
            day=day,
            start_time=start_time,
            end_time=end_time,
            verify_ssl=verify_ssl,
        )
    if not segment:
        # Last resort: check subscribe response (active booking for same area may carry segment).
        try:
            sub_resp = post_json_authed(
                path="/v4/index/subscribe",
                json_body={},
                timeout_sec=float(args.timeout),
                insecure=bool(args.insecure),
                verify_ssl=verify_ssl,
                use_proxy=_effective_use_proxy(args),
            )
            segment = _extract_segment_from_list_resp(
                sub_resp, start_time=start_time, end_time=end_time
            ) or _discover_segment_in_obj(sub_resp, start_time=start_time, end_time=end_time)
        except Exception:  # noqa: BLE001
            pass
    if not segment:
        segment = (env.get("BHLIB_DEFAULT_SEGMENT") or "").strip() or None
    if not segment:
        segment = get_cached_segment(area_id=str(area_id), start_time=start_time, end_time=end_time)
    seats = _extract_seats_from_seat_resp(seat_resp)
    if not seats:
        raise ConfigError("seat 接口没有返回座位列表")

    if not getattr(args, "all", False):
        seats_show = [s for s in seats if str(s.get("status") or "") == "1"]
    else:
        seats_show = seats

    def _s(v) -> str:
        return "" if v is None else str(v)

    # Determine seat_id.
    seat_id: str | None = None
    if args.seat_id:
        seat_id = str(args.seat_id).strip()
    elif args.seat_no:
        seat_no = str(args.seat_no).strip().lstrip("0")
        matches = [s for s in seats if _s(s.get("no")).lstrip("0") == seat_no]
        if not matches:
            raise ConfigError(f"找不到 seat_no={args.seat_no}")
        seat_id = _s(matches[0].get("id"))
    else:
        header = f"area_id={area_id} day={day} {start_time}-{end_time}"
        if segment:
            header += f" segment={segment}"
        print(header)
        print(f"{'id':>7}  {'no':>4}  {'status':>6}  status_name")
        for s in seats_show[:300]:
            print(f"{_s(s.get('id')):>7}  {_s(s.get('no')):>4}  {_s(s.get('status')):>6}  {_s(s.get('status_name'))}")
        if len(seats_show) > 300:
            print(f"... 仅显示前 300 条（总计 {len(seats_show)}）")

        raw = input("选择座位（默认按 seat no；支持 'id:131' / 'no:003'；直接回车取消）：").strip()
        if not raw:
            print("取消")
            return 0

        def _match_by_id(value: str) -> list[dict]:
            return [s for s in seats if _s(s.get("id")) == value]

        def _match_by_no(value: str) -> list[dict]:
            vv = value.lstrip("0")
            return [s for s in seats if _s(s.get("no")).lstrip("0") == vv]

        # Explicit prefixes.
        low = raw.lower()
        if low.startswith(("id:", "id=")):
            value = raw.split(":", 1)[1] if ":" in raw else raw.split("=", 1)[1]
            matches = _match_by_id(value.strip())
            if not matches:
                raise ConfigError(f"找不到 seat id：{value.strip()}")
            seat_id = _s(matches[0].get("id"))
        elif low.startswith(("no:", "no=")):
            value = raw.split(":", 1)[1] if ":" in raw else raw.split("=", 1)[1]
            matches = _match_by_no(value.strip())
            if not matches:
                raise ConfigError(f"找不到 seat no：{value.strip()}")
            seat_id = _s(matches[0].get("id"))
        else:
            # Default: treat raw as seat no (most user-friendly).
            no_matches = _match_by_no(raw)
            if no_matches:
                seat_id = _s(no_matches[0].get("id"))
            else:
                id_matches = _match_by_id(raw)
                if id_matches:
                    seat_id = _s(id_matches[0].get("id"))
                else:
                    raise ConfigError(f"找不到座位：{raw}")

    if not segment:
        # Print the first seat item's keys so we can identify the correct field name.
        _seats_debug = _extract_seats_from_seat_resp(seat_resp)
        sample = _seats_debug[0] if _seats_debug else seat_resp.get("data")
        print("--- seat 响应样本（用于定位 segment 字段）---", file=sys.stderr)
        print(json.dumps(sample, ensure_ascii=False, indent=2), file=sys.stderr)
        print("---", file=sys.stderr)
        raise ConfigError(
            "缺少 segment：seat 响应里没找到（样本已打印到 stderr）。\n"
            "请把 stderr 的输出贴到 issue，或用 `--segment <值>` 临时传入。"
        )
    cache_segment(area_id=str(area_id), start_time=start_time, end_time=end_time, segment=str(segment))

    picked_seat = next((s for s in seats if _s(s.get("id")) == str(seat_id)), None)
    if not picked_seat:
        raise ConfigError("内部错误：找不到所选座位")
    if str(picked_seat.get("status") or "") != "1":
        raise ConfigError(f"所选座位不是空闲状态：status={_s(picked_seat.get('status'))} { _s(picked_seat.get('status_name')) }")

    payload = {
        "seat_id": str(seat_id),
        "segment": str(segment),
        "day": day,
        "start_time": "",
        "end_time": "",
    }
    aesjson = aesjson_encrypt(payload, day=args.crypto_day)
    if args.dry_run:
        print(json.dumps({"payload": payload, "aesjson": aesjson}, ensure_ascii=False, indent=2))
        return 0

    data = post_json_authed(
        path="/v4/space/confirm",
        json_body={"aesjson": aesjson},
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )
    _print_api_result(data)
    return 0


def _cmd_seat_list(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    day = _normalize_day_yyyy_mm_dd(args.day) if args.day else _dt.date.today().isoformat()
    start_time = _normalize_time_hh_mm(
        (args.start_time or _dt.datetime.now().strftime("%H:%M")),
        flag="--start",
    )
    end_time = _normalize_time_hh_mm((args.end_time or "23:00"), flag="--end")

    area_id = _resolve_area_id_maybe(args.area_id, args, auth=auth)
    if not area_id:
        area_id = auth.default_area_id
    if not area_id and args.area_from_subscribe:
        sub = _fetch_subscribe(args, auth, timeout=float(args.timeout), verify_ssl=verify_ssl, insecure=bool(args.insecure))
        item = _pick_my_active_item(sub, prefer_area_id=args.prefer_area_id)
        area_id = str(item.get("area_id") or "").strip() or None
    if not area_id:
        area_id = _interactive_pick_area(args, auth)

    if (args.start_time is None) and (args.end_time is None) and _time_hh_mm_to_minutes(start_time) >= _time_hh_mm_to_minutes(end_time):
        raise ConfigError(
            f"默认时间区间无效：start_time={start_time} end_time={end_time}（当前时间已晚于默认 --end 23:00；请手动指定 --start/--end，格式 HH:MM；日期用 --day YYYY-MM-DD，例如 --day 2026-04-21）"
        )

    payload = {
        "id": str(area_id),
        "day": day,
        "label_id": list(args.label_id or []),
        "start_time": start_time,
        "end_time": end_time,
        "begdate": "",
        "enddate": "",
    }

    data = post_json_authed(
        path="/v4/Space/seat",
        json_body=payload,
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    items = (((data or {}).get("data") or {}).get("list") or []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        raise ConfigError("接口返回结构异常：data.list 不是数组")

    segment = None
    if isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, dict):
            segment = d.get("segment") or d.get("segment_id") or d.get("segmentId")

    include_status = set(args.status or [])
    exclude_status = set(args.not_status or [])
    status_name_contains = (args.status_name_contains or "").strip()

    rows: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        status = str(it.get("status") or "")
        status_name = str(it.get("status_name") or "")
        if include_status and status not in include_status:
            continue
        if exclude_status and status in exclude_status:
            continue
        if status_name_contains and status_name_contains not in status_name:
            continue
        rows.append(it)

    def _s(v) -> str:
        return "" if v is None else str(v)

    seg_part = f" segment={segment}" if segment else ""
    print(f"area_id={area_id} day={day} {start_time}-{end_time}{seg_part} seats={len(rows)}")
    if getattr(args, "show_map", False):
        if getattr(args, "image", False):
            from .seatmap import render_seat_map_to_image

            img_path = render_seat_map_to_image(rows, path=getattr(args, "image_path", None))
            print(f"image={img_path}")
            return 0
        print(render_seat_map(rows))
        return 0
    print(f"{'id':>7}  {'no':>4}  {'status':>6}  status_name")
    for it in rows:
        print(f"{_s(it.get('id')):>7}  {_s(it.get('no')):>4}  {_s(it.get('status')):>6}  {_s(it.get('status_name'))}")
    return 0


def _cmd_book(args: argparse.Namespace) -> int:
    seat = (args.seat or "").strip() if getattr(args, "seat", None) else ""
    if seat:
        if getattr(args, "by_id", False):
            args.seat_id = seat
            args.seat_no = None
        else:
            args.seat_no = seat
            args.seat_id = None
    else:
        args.seat_id = None
        args.seat_no = None
    return _cmd_space_book(args)


def _cmd_light(args: argparse.Namespace) -> int:
    args.brightness = _parse_light_arg(args.value)
    return _cmd_light_set(args)


def _cmd_pomo(args: argparse.Namespace) -> int:
    total_sec = _parse_duration_to_seconds(args.duration)

    low: int | None = args.low
    high: int | None = args.high
    if args.flash:
        parts = str(args.flash).split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ConfigError(f"--flash 格式错误：{args.flash}（应为 LO:HI，例如 20:40）")
        try:
            low = int(parts[0])
            high = int(parts[1])
        except ValueError as e:
            raise ConfigError(f"--flash 的值必须是整数：{args.flash}") from e

    args.low = 20 if low is None else int(low)
    args.high = 40 if high is None else int(high)
    args.seconds = total_sec
    args.minutes = total_sec / 60.0
    return _cmd_pomo_start(args)


def _cmd_pomo_start_daemon(args: argparse.Namespace) -> int:
    """启动后台番茄钟守护进程。"""
    from .pomo_utils import start_daemon
    from .config import save_pomo_state, load_pomo_state, clear_pomo_state
    import time
    from datetime import datetime, timedelta

    # 解析时长
    total_sec = _parse_duration_to_seconds(args.duration)

    # 处理 flash 参数
    low: int | None = args.low
    high: int | None = args.high
    if hasattr(args, 'flash') and args.flash:
        parts = str(args.flash).split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ConfigError(f"--flash 格式错误：{args.flash}（应为 LO:HI，例如 20:40）")
        try:
            low = int(parts[0])
            high = int(parts[1])
        except ValueError as e:
            raise ConfigError(f"--flash 的值必须是整数：{args.flash}") from e

    low = 20 if low is None else int(low)
    high = 40 if high is None else int(high)
    cycles = int(args.cycles) if hasattr(args, 'cycles') else 2
    interval = float(args.interval) if hasattr(args, 'interval') else 0.0

    # 获取当前设备信息（用于状态记录）
    from .pomo_utils import get_current_brightness
    try:
        current_brightness, device_id, area_id = get_current_brightness(
            timeout=args.timeout,
            insecure=args.insecure,
            use_proxy=args.proxy,
            prefer_area_id=args.prefer_area_id,
        )
    except Exception as e:
        raise ConfigError(f"无法获取当前设备信息: {e}")

    # 启动守护进程
    pid = start_daemon(
        duration_seconds=total_sec,
        low=low,
        high=high,
        cycles=cycles,
        interval=interval,
        timeout=args.timeout,
        insecure=args.insecure,
        use_proxy=args.proxy,
        prefer_area_id=args.prefer_area_id,
    )

    # 保存状态
    state = {
        "pid": pid,
        "started_at": datetime.now().isoformat(),
        "duration_seconds": total_sec,
        "end_at": (datetime.now() + timedelta(seconds=total_sec)).isoformat(),
        "original_brightness": current_brightness,
        "device_id": device_id,
        "area_id": area_id,
        "low": low,
        "high": high,
        "cycles": cycles,
        "interval": interval,
        "status": "running",
    }
    save_pomo_state(state)

    print(f"✅ 番茄钟已启动（PID: {pid}）")
    print(f"   时长: {total_sec // 60} 分 {total_sec % 60} 秒")
    print(f"   结束时间: {state['end_at']}")
    print(f"   原始亮度: {current_brightness}")
    print(f"   闪烁: {low}↔{high} x{cycles}")
    print("使用 'bhlib pomo status' 查看状态，'bhlib pomo stop' 提前停止")
    return 0


def _cmd_pomo_status(args: argparse.Namespace) -> int:
    """查看后台番茄钟状态。"""
    from .config import load_pomo_state, is_pomo_running
    from .pomo_utils import calculate_remaining_seconds, format_remaining_time
    import json

    state = load_pomo_state()
    if not state:
        print("没有活跃的番茄钟")
        return 0

    print("番茄钟状态:")
    print(f"   PID: {state.get('pid', 'N/A')}")
    print(f"   开始时间: {state.get('started_at', 'N/A')}")
    print(f"   时长: {state.get('duration_seconds', 0)} 秒")
    print(f"   结束时间: {state.get('end_at', 'N/A')}")
    print(f"   原始亮度: {state.get('original_brightness', 'N/A')}")
    print(f"   设备: {state.get('device_id', 'N/A')} (区域: {state.get('area_id', 'N/A')})")
    print(f"   闪烁设置: {state.get('low', 20)}↔{state.get('high', 40)} x{state.get('cycles', 2)}")
    print(f"   状态: {state.get('status', 'unknown')}")

    # 检查进程是否存活
    if is_pomo_running():
        remaining = calculate_remaining_seconds(state)
        if remaining > 0:
            print(f"   🟢 运行中，剩余: {format_remaining_time(remaining)}")
        else:
            print("   🟡 计时结束，可能正在闪烁或恢复")
    else:
        print("   🔴 进程未运行（可能已结束或崩溃）")
        print("   使用 'bhlib pomo stop' 清理状态")

    return 0


def _cmd_pomo_stop(args: argparse.Namespace) -> int:
    """停止后台番茄钟。"""
    from .config import load_pomo_state, clear_pomo_state, is_pomo_running
    from .pomo_utils import stop_daemon

    state = load_pomo_state()
    if not state:
        print("没有活跃的番茄钟")
        return 0

    pid = state.get('pid')
    if not isinstance(pid, int):
        print("状态中 PID 无效")
        clear_pomo_state()
        return 0

    # 停止进程
    if is_pomo_running():
        print(f"正在停止进程 {pid}...")
        if stop_daemon(pid):
            print("✅ 已发送停止信号")
        else:
            print("⚠️  进程可能已结束")
    else:
        print("进程未在运行")

    # 清理状态
    clear_pomo_state()
    print("状态已清理")
    return 0


def _cmd_pomo_flash_only(args: argparse.Namespace) -> int:
    """立即闪烁灯光（不计时）。"""
    # 复用现有的 _cmd_pomo_flash 但需要适应新的参数结构
    low: int | None = args.low
    high: int | None = args.high
    if hasattr(args, 'flash') and args.flash:
        parts = str(args.flash).split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ConfigError(f"--flash 格式错误：{args.flash}（应为 LO:HI，例如 20:40）")
        try:
            low = int(parts[0])
            high = int(parts[1])
        except ValueError as e:
            raise ConfigError(f"--flash 的值必须是整数：{args.flash}") from e

    args.low = 20 if low is None else int(low)
    args.high = 40 if high is None else int(high)
    args.cycles = int(args.cycles) if hasattr(args, 'cycles') else 2
    args.interval = float(args.interval) if hasattr(args, 'interval') else 0.0

    return _cmd_pomo_flash(args)


def _cmd_pomo_daemon(args: argparse.Namespace) -> int:
    """内部命令：运行番茄钟守护进程。"""
    from .pomo_daemon import main as daemon_main
    return daemon_main(args)




def _cmd_seats(args: argparse.Namespace) -> int:
    # Default to free-only; --all shows everything.
    args.status = [] if getattr(args, "show_all", False) else ["1"]
    # Resolve map vs list: explicit flags win, then config default, then map.
    has_map = getattr(args, "show_map", False)
    has_list = getattr(args, "show_list", False)
    if not has_map and not has_list:
        auth = load_auth_loose()
        if auth.seat_format == "list":
            args.show_list = True
        else:
            args.show_map = True
    # If map output is used (explicit or default), automatically show all seats.
    if getattr(args, "show_map", False):
        args.show_all = True
        args.status = []
    return _cmd_seat_list(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bhlib",
        epilog="全局选项：--version, -V 显示版本信息；--proxy 使用代理；--insecure 跳过 SSL 验证。"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # === login ===
    p_login = sub.add_parser("login", help="北航 SSO 登录（从 .env 读账号密码）")
    p_login.add_argument("--username", help="学号/工号；不传则读 .env 的 BHLIB_USERNAME")
    p_login.add_argument("--password", help="SSO 密码（不传则交互式输入）")
    p_login.add_argument("--seed-cookie", help=argparse.SUPPRESS)
    p_login.add_argument("--base-url", default=None, help=argparse.SUPPRESS)
    p_login.add_argument("--timeout", type=float, default=20.0, help=argparse.SUPPRESS)
    p_login.add_argument("--no-prompt", action="store_true", help="不交互（缺密码时直接报错）")
    p_login.set_defaults(func=_cmd_auth_login, insecure=False)

    # === me ===
    p_me = sub.add_parser("me", help="当前预约/座位状态摘要（--raw 输出完整 JSON）")
    p_me.add_argument("--raw", action="store_true", help="输出完整 subscribe 响应")
    p_me.add_argument("--prefer-area-id", help=argparse.SUPPRESS)
    p_me.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_me.set_defaults(func=_cmd_me, insecure=False)

    # === book ===
    p_book = sub.add_parser(
        "book",
        help="预约座位（无参数：列出空位交互选择；传数字：按座位号直接预约）",
    )
    p_book.add_argument("seat", nargs="?", help="座位号（默认按 no；配合 --id 则为座位 id）")
    p_book.add_argument("--id", dest="by_id", action="store_true", help="把 seat 解释为座位 id 而不是座位号")
    p_book.add_argument("--area", dest="area_id", help="区域（id 或名字，模糊匹配）")
    p_book.add_argument("--day", help="日期 YYYY-MM-DD（也支持 YYYYMMDD；默认今天）")
    p_book.add_argument("--start", dest="start_time", help="开始时间 HH:MM（默认当前时间）")
    p_book.add_argument("--segment", help=argparse.SUPPRESS)
    p_book.add_argument("--all", action="store_true", help="展示所有座位（默认仅空闲）")
    p_book.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    p_book.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_book.set_defaults(
        func=_cmd_book,
        insecure=False,
        end_time=None,
        segment=None,
        crypto_day=None,
    )

    # === signin / leave / checkout ===
    for _name, _func, _help in (
        ("signin", _cmd_space_signin, "签到（到馆）"),
        ("leave", _cmd_space_leave, "暂离"),
        ("checkout", _cmd_space_finish, "离馆"),
    ):
        _p = sub.add_parser(_name, help=_help)
        _p.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
        _p.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
        _p.add_argument("--style", choices=["device_points", "id", "space_id"], default="device_points", help=argparse.SUPPRESS)
        _p.add_argument("--day", help=argparse.SUPPRESS)
        _p.add_argument("--data", help=argparse.SUPPRESS)
        _p.add_argument("--prefer-area-id", help=argparse.SUPPRESS)
        _p.set_defaults(func=_func, insecure=False)

    # === seats ===
    p_seats = sub.add_parser("seats", help="查询空位（默认仅空闲；--all 显示全部）")
    p_seats.add_argument("--area", dest="area_id", help="区域（id 或名字）")
    p_seats.add_argument("--day", help="日期 YYYY-MM-DD（也支持 YYYYMMDD；默认今天）")
    p_seats.add_argument("--start", dest="start_time", help="开始时间 HH:MM（默认当前时间）")
    p_seats.add_argument("--end", dest="end_time", help="结束时间 HH:MM（默认 23:00）")
    p_seats.add_argument("--all", dest="show_all", action="store_true", help="显示全部座位（含已预约/占用）")
    p_seats.add_argument("--map", dest="show_map", action="store_true", help="在终端绘制座位平面图（按状态上色；默认行为）")
    p_seats.add_argument("--list", dest="show_list", action="store_true", help="以列表形式输出座位（而非平面图）")
    p_seats.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_seats.add_argument("--image", dest="image", action="store_true", help="生成座位平面图 PNG 图片（配合 --map 或默认 map 模式）")
    p_seats.add_argument("--image-path", dest="image_path", help="图片保存路径（默认系统临时目录）")
    p_seats.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_seats.set_defaults(
        func=_cmd_seats,
        insecure=False,
        label_id=[],
        prefer_area_id=None,
        area_from_subscribe=False,
        status_name_contains=None,
        not_status=[],
    )

    # === areas ===
    p_areas = sub.add_parser("areas", help="列出所有校区/楼层/区域（树形，结果缓存 24h）")
    p_areas.add_argument("--day", help=argparse.SUPPRESS)
    p_areas.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_areas.add_argument("--flat", action="store_true", help="扁平输出（id  完整路径  free/total）")
    p_areas.add_argument("--refresh", action="store_true", help="跳过缓存，强制重新拉取")
    p_areas.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_areas.set_defaults(func=_cmd_area_list, insecure=False)

    # === light ===
    p_light = sub.add_parser("light", help="设置阅读灯亮度（on=20, off=0, 或传 0-100）")
    p_light.add_argument("value", help="on / off / 0-100")
    p_light.add_argument("--device-id", dest="device_id", help=argparse.SUPPRESS)
    p_light.add_argument("--area-id", dest="area_id", help=argparse.SUPPRESS)
    p_light.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_light.set_defaults(
        func=_cmd_light,
        insecure=False,
        device_id=None,
        area_id=None,
        prefer_area_id=None,
    )

    # === pomo ===
    p_pomo = sub.add_parser("pomo", help="番茄钟命令组")
    sub_pomo = p_pomo.add_subparsers(dest="pomo_cmd", title="子命令", required=True)

    # pomo frontend [duration] [low] [high] (前台模式)
    p_pomo_frontend = sub_pomo.add_parser("frontend", help="前台运行番茄钟（默认 25m，20↔40）")
    p_pomo_frontend.add_argument("duration", nargs="?", default="25m", help="时长：25 / 25m / 1h（默认 25m）")
    p_pomo_frontend.add_argument("low", nargs="?", type=int, default=None, help="低亮度（默认 20）")
    p_pomo_frontend.add_argument("high", nargs="?", type=int, default=None, help="高亮度（默认 40）")
    p_pomo_frontend.add_argument("--flash", help="LO:HI，如 20:40（等价于两个位置参数）")
    p_pomo_frontend.add_argument("--cycles", type=int, default=2, help="到达高亮度的次数（默认 2）")
    p_pomo_frontend.add_argument("--interval", type=float, default=0.0, help=argparse.SUPPRESS)
    p_pomo_frontend.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_pomo_frontend.add_argument("--insecure", action="store_true", help="跳过 SSL 证书验证")
    p_pomo_frontend.add_argument("--proxy", action="store_true", help="使用系统代理")
    p_pomo_frontend.add_argument("--prefer-area-id", type=str, default=None, help=argparse.SUPPRESS)
    p_pomo_frontend.set_defaults(func=_cmd_pomo, insecure=False, prefer_area_id=None)

    # pomo start [duration] [low] [high] [--flash=LO:HI] [--cycles=CYCLES]
    p_pomo_start = sub_pomo.add_parser("start", help="后台启动番茄钟守护进程")
    p_pomo_start.add_argument("duration", nargs="?", default="25m", help="时长：25 / 25m / 1h（默认 25m）")
    p_pomo_start.add_argument("low", nargs="?", type=int, default=None, help="闪烁低亮度（默认 20）")
    p_pomo_start.add_argument("high", nargs="?", type=int, default=None, help="闪烁高亮度（默认 40）")
    p_pomo_start.add_argument("--flash", help="LO:HI，如 20:40（等价于两个位置参数）")
    p_pomo_start.add_argument("--cycles", type=int, default=2, help="闪烁次数（默认 2）")
    p_pomo_start.add_argument("--interval", type=float, default=0.0, help=argparse.SUPPRESS)
    p_pomo_start.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_pomo_start.add_argument("--insecure", action="store_true", help="跳过 SSL 证书验证")
    p_pomo_start.add_argument("--proxy", action="store_true", help="使用系统代理")
    p_pomo_start.add_argument("--prefer-area-id", type=str, default=None, help=argparse.SUPPRESS)
    p_pomo_start.set_defaults(func=_cmd_pomo_start_daemon, insecure=False, prefer_area_id=None)

    # pomo status
    p_pomo_status = sub_pomo.add_parser("status", help="查看后台番茄钟状态")
    p_pomo_status.set_defaults(func=_cmd_pomo_status, insecure=False)

    # pomo stop
    p_pomo_stop = sub_pomo.add_parser("stop", help="停止后台番茄钟")
    p_pomo_stop.set_defaults(func=_cmd_pomo_stop, insecure=False)

    # pomo flash [--low=LOW] [--high=HIGH] [--cycles=CYCLES]
    p_pomo_flash = sub_pomo.add_parser("flash", help="立即闪烁灯光（不启动计时器）")
    p_pomo_flash.add_argument("--low", type=int, default=20, help="低亮度（默认 20）")
    p_pomo_flash.add_argument("--high", type=int, default=40, help="高亮度（默认 40）")
    p_pomo_flash.add_argument("--cycles", type=int, default=2, help="闪烁次数（默认 2）")
    p_pomo_flash.add_argument("--interval", type=float, default=0.0, help=argparse.SUPPRESS)
    p_pomo_flash.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_pomo_flash.add_argument("--insecure", action="store_true", help="跳过 SSL 证书验证")
    p_pomo_flash.add_argument("--proxy", action="store_true", help="使用系统代理")
    p_pomo_flash.add_argument("--prefer-area-id", type=str, default=None, help=argparse.SUPPRESS)
    p_pomo_flash.set_defaults(func=_cmd_pomo_flash_only, insecure=False, prefer_area_id=None)

    # === hidden: pomo-daemon (internal use only) ===
    p_pomo_daemon = sub.add_parser("pomo-daemon", add_help=False)
    p_pomo_daemon.add_argument("--duration", type=float, required=True, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--low", type=int, default=20, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--high", type=int, default=40, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--cycles", type=int, default=2, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--interval", type=float, default=0.0, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--prefer-area-id", type=str, default=None, help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--proxy", action="store_true", help=argparse.SUPPRESS)
    p_pomo_daemon.add_argument("--record-brightness", action="store_true", help=argparse.SUPPRESS)
    p_pomo_daemon.set_defaults(func=_cmd_pomo_daemon, insecure=False)

    # === config ===
    p_config = sub.add_parser("config", help=f"写入默认值到 {CONFIG_FILE}（如默认区域）")
    p_config.add_argument("--default-area", dest="default_area_id", help="常用区域（id 或名字）")
    p_config.add_argument("--seat-format", choices=["map", "list"], help="seats 命令默认输出格式（map=平面图，list=列表）")
    p_config.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p_config.set_defaults(func=_prefs_set, insecure=False)

    # === hidden: auth / crypto (no help= → omitted from --help) ===
    p_auth = sub.add_parser("auth")
    sub_auth = p_auth.add_subparsers(dest="auth_cmd", required=True)

    p_auth_set = sub_auth.add_parser("set")
    p_auth_set.add_argument("--token", required=True)
    p_auth_set.add_argument("--cookie", required=True)
    p_auth_set.add_argument("--base-url", default=None)
    p_auth_set.add_argument("--insecure", action="store_true")
    p_auth_set.set_defaults(func=_cmd_auth_set)

    p_auth_show = sub_auth.add_parser("show")
    p_auth_show.set_defaults(func=_cmd_auth_show)

    p_auth_clear = sub_auth.add_parser("clear")
    p_auth_clear.set_defaults(func=_cmd_auth_clear)

    p_crypto = sub.add_parser("crypto")
    sub_crypto = p_crypto.add_subparsers(dest="crypto_cmd", required=True)

    p_crypto_enc = sub_crypto.add_parser("encrypt")
    p_crypto_enc.add_argument("--day")
    p_crypto_enc.add_argument("--data", required=True)
    p_crypto_enc.set_defaults(func=_cmd_crypto_encrypt)

    p_crypto_dec = sub_crypto.add_parser("decrypt")
    p_crypto_dec.add_argument("--day")
    p_crypto_dec.add_argument("--aesjson", required=True)
    p_crypto_dec.add_argument("--json", action="store_true")
    p_crypto_dec.set_defaults(func=_cmd_crypto_decrypt)

    return parser


def _resolve_area_id_maybe(arg: str | None, args: argparse.Namespace, *, auth=None) -> str | None:
    """
    Resolve --area-id argument: numeric → as-is (no network), else fuzzy-match
    via area tree. Pass-through for None/empty.
    """
    if arg is None:
        return None
    s = str(arg).strip()
    if not s:
        return None
    if s.isdigit():
        return s
    auth = auth if auth is not None else load_auth_loose()
    return resolve_area_id(
        s,
        timeout_sec=float(getattr(args, "timeout", 15.0)),
        insecure=bool(getattr(args, "insecure", False)),
        verify_ssl=_effective_verify_ssl(auth, args),
        use_proxy=_effective_use_proxy(args),
    )


def _cmd_area_list(args: argparse.Namespace) -> int:
    auth = load_auth_loose()
    verify_ssl = _effective_verify_ssl(auth, args)
    tree = get_or_fetch_tree(
        refresh=bool(args.refresh),
        day=args.day,
        timeout_sec=float(args.timeout),
        insecure=bool(args.insecure),
        verify_ssl=verify_ssl,
        use_proxy=_effective_use_proxy(args),
    )

    if args.json:
        print(json.dumps(tree, ensure_ascii=False, indent=2))
        return 0

    if args.flat:
        for a in flatten_areas(tree):
            print(f"{a['id']:>4}  {a['nameMerge']}  free={a['free_num']}/{a['total_num']}  [{a['typeName']}]")
        return 0

    print(f"day={tree['day']}")
    for pr in tree["premises"]:
        print(f"◆ {pr['name']}  id={pr['id']}  free {pr['free_num']}/{pr['total_num']}")
        for st in pr["storeys"]:
            print(f"  ├ {st['name']}  id={st['id']}  free {st['free_num']}/{st['total_num']}")
            for a in st["areas"]:
                print(f"  │    {a['id']:>4}  {a['name']}  free={a['free_num']}/{a['total_num']}  [{a['typeName']}]")
    return 0


def _prefs_set(args: argparse.Namespace) -> int:
    if args.default_area_id is None and args.seat_format is None:
        raise ConfigError("请至少传一个字段（例如 --default-area 8 或 --seat-format map）")
    resolved = _resolve_area_id_maybe(args.default_area_id, args) if args.default_area_id is not None else None
    update_defaults(default_area_id=resolved, seat_format=args.seat_format)
    parts: list[str] = []
    if resolved is not None:
        parts.append(f"default_area_id={resolved}")
    if args.seat_format is not None:
        parts.append(f"seat_format={args.seat_format}")
    msg = f"OK: 已更新 {CONFIG_FILE} ({', '.join(parts)})"
    if resolved is not None and resolved != args.default_area_id:
        msg += f"  ← default_area_id 解析自 '{args.default_area_id}'"
    print(msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    # Handle --version before any other processing
    if "--version" in raw_argv or "-V" in raw_argv:
        from importlib.metadata import version
        try:
            print(f"bhlib {version('bhlib')}")
        except Exception:
            # fallback to the version defined in the package
            from bhlib import __version__
            print(f"bhlib {__version__}")
        return 0

    # Global flags that apply to any subcommand; we strip them before argparse.
    use_proxy = "--proxy" in raw_argv
    insecure = "--insecure" in raw_argv
    raw_argv = [a for a in raw_argv if a not in ("--proxy", "--insecure")]
    if use_proxy:
        os.environ["BHLIB_PROXY"] = "1"
    if insecure:
        os.environ["BHLIB_INSECURE"] = "1"

    parser = build_parser()
    if not raw_argv:
        parser.print_help()
        return 0
    args = parser.parse_args(raw_argv)
    if use_proxy:
        setattr(args, "proxy", True)
    if insecure:
        setattr(args, "insecure", True)
    try:
        return int(args.func(args))
    except (ConfigError, HttpError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
