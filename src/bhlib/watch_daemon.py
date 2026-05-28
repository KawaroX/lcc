#!/usr/bin/env python3
"""座位监测守护进程 - 后台轮询、diff、记录、推送通知。

此模块作为内部命令由 `bhlib watch start` 启动，不应直接调用。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import signal
import sys
import time
import traceback

from . import watch
from .api import post_json_authed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="座位监测守护进程（内部使用）",
        add_help=False,
    )
    parser.add_argument("--area-id", required=True, help="区域 ID")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--proxy", action="store_true")
    return parser.parse_args()


def fetch_seats(area_id: str, *, timeout: float, insecure: bool, use_proxy: bool) -> list[dict]:
    today = _dt.date.today().isoformat()
    payload = {
        "id": str(area_id),
        "day": today,
        "label_id": [],
        # 用全天范围，让接口尽可能返回全部座位的实时状态。
        "start_time": "00:00",
        "end_time": "23:59",
        "begdate": "",
        "enddate": "",
    }
    resp = post_json_authed(
        path="/v4/Space/seat",
        json_body=payload,
        timeout_sec=timeout,
        insecure=insecure,
        verify_ssl=(not insecure),
        use_proxy=use_proxy,
    )
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if not isinstance(data, dict):
        return []
    lst = data.get("list")
    if not isinstance(lst, list):
        return []
    return [it for it in lst if isinstance(it, dict)]


def log_line(msg: str) -> None:
    watch.ensure_data_dir()
    line = f"[{_dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    try:
        with watch.log_file().open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    # 同时打到 stderr，方便 launchd / journalctl
    print(line.rstrip(), file=sys.stderr, flush=True)


def tick_once(area_id: str, *, timeout: float, insecure: bool, use_proxy: bool) -> tuple[int, int]:
    """跑一次：抓快照、diff、写事件、推通知、保存 state。

    返回 (events_count, notifications_count)。
    """
    from .config import load_watch_config

    cfg = load_watch_config()
    seats = fetch_seats(area_id, timeout=timeout, insecure=insecure, use_proxy=use_proxy)
    if not seats:
        log_line("WARN seats API 返回为空，跳过本轮")
        return 0, 0
    now = _dt.datetime.now()
    state = watch.load_state()
    # 如果区域换了，丢弃旧 state，重新积累（避免误判 diff）。
    if state and str(state.get("area_id") or "") != str(area_id):
        log_line(f"INFO 区域从 {state.get('area_id')} 切到 {area_id}，重置 state")
        state = {}
    new_state, events = watch.diff_and_update(
        state, area_id=str(area_id), seats=seats, now=now
    )
    n_events = watch.append_events(events)
    # 通知分发可能就地修改 new_state（标记 expire_notified）
    sent = watch.dispatch_notifications(
        events=events, state=new_state, config=cfg, now=now
    )
    watch.save_state(new_state)
    return n_events, len(sent)


_stop_flag = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _stop_flag
    _stop_flag = True
    log_line(f"INFO 收到信号 {signum}，准备退出")


def main(args: argparse.Namespace | None = None) -> int:
    if args is None:
        args = parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 崩溃恢复：上次没跑 stop 钩子的话，补一条 stop 进会话日志
    recovered = watch.recover_crashed_session()
    if recovered is not None:
        log_line(f"INFO 检测到崩溃残留，已补写 stop@{recovered.isoformat(timespec='seconds')}")

    boot_ts = _dt.datetime.now()
    watch.record_session_start(boot_ts)
    watch.update_last_tick(boot_ts)

    log_line(
        f"INFO watch daemon 启动 area={args.area_id} poll={args.poll_seconds}s "
        f"pid={__import__('os').getpid()}"
    )

    while not _stop_flag:
        try:
            n_ev, n_notify = tick_once(
                str(args.area_id),
                timeout=float(args.timeout),
                insecure=bool(args.insecure),
                use_proxy=bool(args.proxy),
            )
            # 心跳：tick 走完一轮就更新一次
            watch.update_last_tick(_dt.datetime.now())
            if n_ev or n_notify:
                log_line(f"INFO tick events={n_ev} notify={n_notify}")
        except Exception as e:  # noqa: BLE001
            log_line(f"ERROR tick 失败: {e}")
            log_line(traceback.format_exc())

        # 分段 sleep，便于及时响应信号
        slept = 0.0
        step = 1.0
        while slept < args.poll_seconds and not _stop_flag:
            time.sleep(step)
            slept += step

    # 收尾：用最后心跳的时间作为 stop ts（精确到最后一次 tick）
    stop_ts = watch._read_last_tick() or _dt.datetime.now()
    watch.record_session_stop(stop_ts)
    log_line(f"INFO watch daemon 退出，session stop@{stop_ts.isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
