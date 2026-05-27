"""座位变化监测：拉取快照、diff、写事件流、推送通知。

设计要点：
- watch_state.json: 最新一帧 + 每个座位"进入当前状态的时间" / 到期点 / 是否已经发过"即将到期"提醒
- watch_events.jsonl: 状态变迁的 append-only 日志（用于以后分析）
- leave_window_minutes: 进入临时离开的时刻落在 10:30-13:30 / 16:30-19:00 闭区间 → 120 分钟，否则 30 分钟
- 通知按类型独立开关；忽略名单只静音通知，事件仍然记录
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import DATA_DIR

SCHEMA_VERSION = 1
STATUS_FREE = "1"
STATUS_RESERVED = "2"
STATUS_IN_USE = "6"
STATUS_TEMP_LEAVE = "7"
STATUS_NAME = {
    STATUS_FREE: "空闲",
    STATUS_RESERVED: "已预约",
    STATUS_IN_USE: "使用中",
    STATUS_TEMP_LEAVE: "临时离开",
}


def state_file() -> Path:
    return DATA_DIR / "watch_state.json"


def events_file() -> Path:
    return DATA_DIR / "watch_events.jsonl"


def log_file() -> Path:
    return DATA_DIR / "watch.log"


def pid_file() -> Path:
    return DATA_DIR / "watch.pid"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 时间 / 窗口
# --------------------------------------------------------------------------- #

# 闭区间 [10:30, 13:30] 和 [16:30, 19:00]。
# 在这两段窗口里"开始临时离开"的，馆方允许 2h；其余时间 30min。
_LONG_WINDOWS = (
    ((10, 30), (13, 30)),
    ((16, 30), (19, 0)),
)


def leave_window_minutes(at: _dt.datetime) -> int:
    """返回 at 时刻进入临时离开应得的分钟阈值（120 或 30）。"""
    hm = (at.hour, at.minute)
    for lo, hi in _LONG_WINDOWS:
        if lo <= hm <= hi:
            return 120
    return 30


def compute_expire_at(since: _dt.datetime) -> _dt.datetime:
    return since + _dt.timedelta(minutes=leave_window_minutes(since))


# --------------------------------------------------------------------------- #
# 状态 / 事件
# --------------------------------------------------------------------------- #


@dataclass
class Event:
    ts: _dt.datetime
    area_id: str
    seat_id: str
    seat_no: str
    from_status: str | None  # None = first observation
    to_status: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(timespec="seconds"),
            "area": self.area_id,
            "seat_id": self.seat_id,
            "seat_no": self.seat_no,
            "from": self.from_status,
            "to": self.to_status,
        }


def load_state() -> dict:
    p = state_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    ensure_data_dir()
    tmp = state_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_file())


def append_events(events: Iterable[Event]) -> int:
    events = list(events)
    if not events:
        return 0
    ensure_data_dir()
    with events_file().open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
    return len(events)


def iter_events(*, since: _dt.datetime | None = None) -> Iterable[dict]:
    """按行迭代事件 jsonl。`since` 若给定，则按 ts 过滤。"""
    p = events_file()
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                try:
                    ts = _dt.datetime.fromisoformat(ev.get("ts") or "")
                except ValueError:
                    continue
                if ts < since:
                    continue
            yield ev


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def diff_and_update(
    state: dict,
    *,
    area_id: str,
    seats: list[dict],
    now: _dt.datetime,
) -> tuple[dict, list[Event]]:
    """返回 (new_state, events)。state 是上一次 watch_state.json 的内容。

    seats 是从 /v4/Space/seat 接口拿到的座位列表，每个元素至少包含 id/no/status。
    """
    old_seats = (state.get("seats") if isinstance(state.get("seats"), dict) else {}) or {}
    new_seats: dict[str, dict] = {}
    events: list[Event] = []

    for it in seats:
        if not isinstance(it, dict):
            continue
        seat_id = str(it.get("id") or "").strip()
        if not seat_id:
            continue
        seat_no = str(it.get("no") or "").strip()
        status = str(it.get("status") or "").strip()
        if not status:
            continue

        prev = old_seats.get(seat_id) if isinstance(old_seats.get(seat_id), dict) else None
        prev_status = prev.get("status") if prev else None

        if prev_status == status:
            # 复制旧元数据（since / expire_at / expire_notified）
            new_seats[seat_id] = {
                "no": seat_no or (prev.get("no") if prev else ""),
                "status": status,
                "since": prev.get("since"),
                "expire_at": prev.get("expire_at"),
                "expire_notified": bool(prev.get("expire_notified")),
            }
            continue

        # 状态变化或首次观测
        if prev_status is not None:
            events.append(
                Event(
                    ts=now,
                    area_id=area_id,
                    seat_id=seat_id,
                    seat_no=seat_no,
                    from_status=prev_status,
                    to_status=status,
                )
            )
        # 首次观测不写 event，避免每次启动塞一堆"from=null"的噪音。
        # 但 since/expire_at 仍然按"我们第一次看到的时间"算。
        expire_at = (
            compute_expire_at(now).isoformat(timespec="seconds")
            if status == STATUS_TEMP_LEAVE
            else None
        )
        new_seats[seat_id] = {
            "no": seat_no,
            "status": status,
            "since": now.isoformat(timespec="seconds"),
            "expire_at": expire_at,
            "expire_notified": False,
        }

    new_state = {
        "schema": SCHEMA_VERSION,
        "area_id": area_id,
        "updated_at": now.isoformat(timespec="seconds"),
        "first_seen_at": state.get("first_seen_at") or now.isoformat(timespec="seconds"),
        "tick_count": int(state.get("tick_count") or 0) + 1,
        "seats": new_seats,
    }
    return new_state, events


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #


def _osascript_notify(title: str, body: str) -> None:
    osa = "/usr/bin/osascript"
    if not os.path.exists(osa):
        return
    script = (
        'on run argv\n'
        '  display notification (item 1 of argv) with title (item 2 of argv)\n'
        'end run'
    )
    subprocess.run(
        [osa, "-e", script, body, title],
        capture_output=True,
        text=True,
        check=False,
    )


def _notify_send(title: str, body: str) -> None:
    bin_ = shutil.which("notify-send")
    if not bin_:
        return
    subprocess.run([bin_, title, body], capture_output=True, check=False)


def _powershell_notify(title: str, body: str) -> None:
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return
    script = (
        "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;"
        "[reflection.assembly]::loadwithpartialname('System.Drawing') | Out-Null;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        f"$n.BalloonTipTitle='{title}';$n.BalloonTipText='{body}';"
        "$n.Visible=$true;$n.ShowBalloonTip(5000);Start-Sleep -Seconds 6"
    )
    subprocess.run([ps, "-NoProfile", "-Command", script], capture_output=True, check=False)


def notify(title: str, body: str) -> None:
    """跨平台 best-effort 通知；找不到合适后端就静默丢弃。"""
    plat = sys.platform
    if plat == "darwin":
        _osascript_notify(title, body)
    elif plat.startswith("linux"):
        _notify_send(title, body)
    elif plat == "win32":
        _powershell_notify(title, body)


# --------------------------------------------------------------------------- #
# 事件 -> 通知分发
# --------------------------------------------------------------------------- #


def _classify(ev: Event) -> str | None:
    """把事件归到通知类型。返回 None 表示"不通知"。"""
    if ev.to_status == STATUS_FREE:
        return "new_free"
    if ev.to_status == STATUS_IN_USE:
        return "taken"
    if ev.to_status == STATUS_TEMP_LEAVE:
        return "temp_leave"
    return None


def _format_remaining(delta: _dt.timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "已过期"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}min"
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}min"


def dispatch_notifications(
    *,
    events: list[Event],
    state: dict,
    config: dict,
    now: _dt.datetime,
) -> list[tuple[str, str]]:
    """根据 events 和 state 分发通知；返回 [(title, body), ...] 给调用方记日志用。

    会就地修改 state 来记录 expire_soon 已通知过的座位。
    """
    ignore = set(config.get("ignore_seats") or [])
    notify_cfg = config.get("notify") or {}
    sent: list[tuple[str, str]] = []

    # 1) 按类型批量发：把同 tick 的同类事件合并为一条通知。
    buckets: dict[str, list[Event]] = {}
    for ev in events:
        cls = _classify(ev)
        if cls is None:
            continue
        if ev.seat_no in ignore or ev.seat_id in ignore:
            continue
        if not notify_cfg.get(cls, False):
            continue
        buckets.setdefault(cls, []).append(ev)

    for cls, evs in buckets.items():
        nos = ", ".join(sorted({e.seat_no or e.seat_id for e in evs}))
        if cls == "new_free":
            title = f"新增 {len(evs)} 个空位"
        elif cls == "taken":
            title = f"{len(evs)} 个空位被使用"
        elif cls == "temp_leave":
            title = f"{len(evs)} 个新临时离开"
        else:
            title = f"{cls} × {len(evs)}"
        body = nos
        notify(title, body)
        sent.append((title, body))

    # 2) expire_soon：扫 state.seats，找出 7 状态且即将到期但还没通知过的。
    warn_minutes = int(config.get("expire_warn_minutes") or 5)
    if notify_cfg.get("expire_soon", False):
        soon: list[tuple[str, str, _dt.timedelta]] = []  # (seat_no, seat_id, remaining)
        for seat_id, sd in (state.get("seats") or {}).items():
            if not isinstance(sd, dict):
                continue
            if sd.get("status") != STATUS_TEMP_LEAVE:
                continue
            if sd.get("expire_notified"):
                continue
            seat_no = str(sd.get("no") or "")
            if seat_no in ignore or seat_id in ignore:
                continue
            expire_at_s = sd.get("expire_at")
            if not expire_at_s:
                continue
            try:
                expire_at = _dt.datetime.fromisoformat(expire_at_s)
            except ValueError:
                continue
            remaining = expire_at - now
            if remaining <= _dt.timedelta(minutes=warn_minutes):
                soon.append((seat_no, seat_id, remaining))
                sd["expire_notified"] = True

        if soon:
            soon.sort(key=lambda t: t[2])
            title = f"{len(soon)} 个临时离开即将到期"
            body = "; ".join(
                f"{no or sid} 剩 {_format_remaining(rem)}" for no, sid, rem in soon
            )
            notify(title, body)
            sent.append((title, body))

    return sent


# --------------------------------------------------------------------------- #
# 给 seats 命令用的回看接口
# --------------------------------------------------------------------------- #


def spawn_daemon(
    *,
    area_id: str,
    poll_seconds: int,
    timeout: float = 15.0,
    insecure: bool = False,
    use_proxy: bool = False,
) -> int:
    """启动后台 watch 守护进程，返回 PID。"""
    ensure_data_dir()
    cmd = [
        sys.executable, "-m", "bhlib", "watch-daemon",
        "--area-id", str(area_id),
        "--poll-seconds", str(int(poll_seconds)),
        "--timeout", str(timeout),
    ]
    if insecure:
        cmd.append("--insecure")
    if use_proxy:
        cmd.append("--proxy")

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    else:
        kwargs["start_new_session"] = True

    log = log_file()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"--- spawn at {_dt.datetime.now().isoformat(timespec='seconds')} ---\n")
        f.write(f"cmd: {' '.join(cmd)}\n")
    with log.open("a", encoding="utf-8") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=f, **kwargs)
    return proc.pid


def is_process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
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
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def signal_stop(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GenerateConsoleCtrlEvent(1, pid)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError):
        return False


def temp_leave_countdown(*, area_id: str, now: _dt.datetime | None = None) -> list[dict]:
    """返回 [{seat_no, seat_id, expire_at, remaining_seconds}, ...]，按剩余时间升序。

    没有快照、区域不匹配或没有 7 状态座位 → 返回 []。
    """
    now = now or _dt.datetime.now()
    state = load_state()
    if not state or str(state.get("area_id") or "") != str(area_id):
        return []
    seats = state.get("seats") or {}
    out: list[dict] = []
    for seat_id, sd in seats.items():
        if not isinstance(sd, dict):
            continue
        if sd.get("status") != STATUS_TEMP_LEAVE:
            continue
        expire_at_s = sd.get("expire_at")
        if not expire_at_s:
            continue
        try:
            expire_at = _dt.datetime.fromisoformat(expire_at_s)
        except ValueError:
            continue
        out.append(
            {
                "seat_id": seat_id,
                "seat_no": str(sd.get("no") or ""),
                "expire_at": expire_at,
                "remaining_seconds": int((expire_at - now).total_seconds()),
                "since": sd.get("since"),
            }
        )
    out.sort(key=lambda r: r["remaining_seconds"])
    return out
