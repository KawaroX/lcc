#!/usr/bin/env python3
"""
番茄钟守护进程 - 后台运行，计时结束后闪烁灯光并恢复原始亮度。

此模块作为内部命令由 `lcc pomo start` 启动，不应直接调用。
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from .auth import load_auth_loose
from .cli import _flash_light_brightness, _fetch_subscribe, _pick_my_light_device
from .cli import _effective_verify_ssl, _effective_use_proxy
from .config import ConfigError
from .pomo_utils import get_current_brightness, set_brightness


class DaemonError(Exception):
    """守护进程专用错误"""
    pass


def parse_args() -> argparse.Namespace:
    """解析守护进程命令行参数。"""
    parser = argparse.ArgumentParser(
        description="番茄钟守护进程（内部使用）",
        add_help=False,
    )

    parser.add_argument(
        "--duration", type=float, required=True,
        help="番茄钟时长（秒）",
    )
    parser.add_argument(
        "--low", type=int, default=20,
        help="闪烁低亮度（默认 20）",
    )
    parser.add_argument(
        "--high", type=int, default=40,
        help="闪烁高亮度（默认 40）",
    )
    parser.add_argument(
        "--cycles", type=int, default=2,
        help="闪烁次数（默认 2）",
    )
    parser.add_argument(
        "--interval", type=float, default=0.0,
        help="闪烁间隔（秒，默认 0）",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="API 超时时间（秒，默认 15）",
    )
    parser.add_argument(
        "--prefer-area-id", type=str, default=None,
        help="偏好区域 ID",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="跳过 SSL 证书验证",
    )
    parser.add_argument(
        "--proxy", action="store_true",
        help="使用系统代理",
    )

    # 内部参数
    parser.add_argument(
        "--record-brightness", action="store_true",
        help=argparse.SUPPRESS,  # 内部使用：立即记录当前亮度并退出
    )

    return parser.parse_args()


def record_original_brightness(args: argparse.Namespace) -> tuple[int, str, str]:
    """记录当前亮度，返回 (brightness, device_id, area_id)。"""
    try:
        brightness, device_id, area_id = get_current_brightness(
            timeout=args.timeout,
            insecure=args.insecure,
            use_proxy=args.proxy,
            prefer_area_id=args.prefer_area_id,
        )
        print(f"DEBUG: 记录当前亮度 {brightness} (设备 {device_id}, 区域 {area_id})", file=sys.stderr)
        return brightness, device_id, area_id
    except Exception as e:
        raise DaemonError(f"无法记录当前亮度: {e}")


def restore_brightness(
    brightness: int,
    device_id: str,
    area_id: str,
    args: argparse.Namespace,
) -> None:
    """恢复原始亮度。"""
    try:
        print(f"DEBUG: 恢复亮度到 {brightness}", file=sys.stderr)
        set_brightness(
            brightness=brightness,
            device_id=device_id,
            area_id=area_id,
            timeout=args.timeout,
            insecure=args.insecure,
            use_proxy=args.proxy,
        )
    except Exception as e:
        print(f"警告: 恢复亮度失败: {e}", file=sys.stderr)


def wait_for_timer(duration: float) -> bool:
    """等待指定时长，可被信号中断。

    返回: True 如果完整等待，False 如果被中断
    """
    end_time = time.time() + duration
    interrupted = False

    def handle_signal(signum, frame):
        nonlocal interrupted
        interrupted = True
        print(f"收到信号 {signum}，准备退出...", file=sys.stderr)

    # 注册信号处理
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while time.time() < end_time and not interrupted:
            remaining = end_time - time.time()
            # 最多睡眠 1 秒，以便及时响应信号
            sleep_time = min(1.0, remaining)
            if sleep_time > 0:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        interrupted = True

    return not interrupted


def main(args: Optional[argparse.Namespace] = None) -> int:
    """守护进程主函数。"""
    if args is None:
        args = parse_args()

    # 特殊模式：仅记录亮度（用于测试）
    if args.record_brightness:
        try:
            brightness, device_id, area_id = record_original_brightness(args)
            print(f"{brightness} {device_id} {area_id}")
            return 0
        except Exception as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1

    original_brightness = None
    device_id = None
    area_id = None

    try:
        print(f"番茄钟守护进程启动，时长 {args.duration} 秒", file=sys.stderr)
        print(f"预计结束时间: {(datetime.now() + timedelta(seconds=args.duration)).strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)

        # 等待计时结束（可被中断）
        completed = wait_for_timer(args.duration)

        if completed:
            print("计时结束，准备闪烁灯光...", file=sys.stderr)
            # 记录当前亮度（关键：闪烁前记录）
            original_brightness, device_id, area_id = record_original_brightness(args)

            # 执行闪烁
            print(f"闪烁灯光: {args.low}->{args.high} x{args.cycles}", file=sys.stderr)
            flash_args = SimpleNamespace(
                timeout=args.timeout,
                insecure=args.insecure,
                proxy=args.proxy,
                prefer_area_id=args.prefer_area_id,
                low=args.low,
                high=args.high,
                cycles=args.cycles,
                interval=args.interval,
            )
            _flash_light_brightness(
                flash_args,
                low=args.low,
                high=args.high,
                cycles=args.cycles,
                interval=args.interval,
            )
            print("闪烁完成", file=sys.stderr)
        else:
            print("番茄钟被中断", file=sys.stderr)

    except DaemonError as e:
        print(f"守护进程错误: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("收到键盘中断", file=sys.stderr)
    except Exception as e:
        print(f"未预期的错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        # 恢复原始亮度（如果已记录）
        if original_brightness is not None and device_id is not None and area_id is not None:
            print(f"恢复原始亮度 {original_brightness}", file=sys.stderr)
            restore_brightness(original_brightness, device_id, area_id, args)
        else:
            print("未记录原始亮度，跳过恢复", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())