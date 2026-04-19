from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .auth import load_auth_loose
from .config import ConfigError, _config_path
from .cli import _fetch_subscribe, _pick_my_light_device, _set_light_brightness_by_ids


def get_current_brightness(
    timeout: float = 15.0,
    insecure: bool = False,
    use_proxy: bool = False,
    prefer_area_id: Optional[str] = None,
) -> tuple[int, str, str]:
    """获取当前座位的亮度值、设备ID和区域ID。

    返回: (brightness, device_id, area_id)
    异常: ConfigError 如果找不到灯光设备或亮度值无效
    """
    auth = load_auth_loose()

    # 复用 CLI 中的逻辑获取当前设备信息
    from .cli import _effective_verify_ssl
    verify_ssl = _effective_verify_ssl(auth, type('Args', (), {'insecure': insecure})())

    # 获取 subscribe 数据
    subscribe_resp = _fetch_subscribe(
        type('Args', (), {
            'insecure': insecure,
            'timeout': timeout,
            'prefer_area_id': prefer_area_id,
        })(),
        auth,
        timeout=timeout,
        verify_ssl=verify_ssl,
        insecure=insecure,
    )

    # 选取灯光设备
    device_info = _pick_my_light_device(subscribe_resp, prefer_area_id=prefer_area_id)

    device_id = str(device_info.get('id') or '')
    area_id = str(device_info.get('area_id') or '')
    brightness = device_info.get('brightness')

    if brightness is None:
        # 如果 API 未返回亮度，默认设为 0（关灯状态）
        brightness = 0

    try:
        brightness_int = int(brightness)
        if not 0 <= brightness_int <= 100:
            raise ConfigError(f"亮度值无效: {brightness_int}（应在 0-100 范围内）")
    except (ValueError, TypeError):
        raise ConfigError(f"亮度值不是有效整数: {brightness}")

    return brightness_int, device_id, area_id


def set_brightness(
    brightness: int,
    device_id: str,
    area_id: str,
    timeout: float = 15.0,
    insecure: bool = False,
    use_proxy: bool = False,
) -> None:
    """设置指定设备的亮度。"""
    if not 0 <= brightness <= 100:
        raise ConfigError(f"亮度值无效: {brightness}（应在 0-100 范围内）")

    auth = load_auth_loose()
    from .cli import _effective_verify_ssl
    verify_ssl = _effective_verify_ssl(auth, type('Args', (), {'insecure': insecure})())

    _set_light_brightness_by_ids(
        device_id=device_id,
        area_id=area_id,
        brightness=brightness,
        timeout=timeout,
        insecure=insecure,
        verify_ssl=verify_ssl,
        use_proxy=use_proxy,
    )


def calculate_remaining_seconds(state: dict) -> float:
    """计算番茄钟剩余时间（秒）。"""
    if state.get('status') not in ('running', 'flashing'):
        return 0.0

    end_at_str = state.get('end_at')
    if not end_at_str:
        return 0.0

    try:
        end_at = datetime.fromisoformat(end_at_str.replace('Z', '+00:00'))
        now = datetime.now(end_at.tzinfo) if end_at.tzinfo else datetime.now()
        remaining = (end_at - now).total_seconds()
        return max(0.0, remaining)
    except (ValueError, TypeError):
        return 0.0


def format_remaining_time(seconds: float) -> str:
    """格式化剩余时间为可读字符串。"""
    if seconds <= 0:
        return "已结束"

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    elif minutes > 0:
        return f"{minutes}分{secs:02d}秒"
    else:
        return f"{secs}秒"


def start_daemon(
    duration_seconds: float,
    low: int = 20,
    high: int = 40,
    cycles: int = 2,
    interval: float = 0.0,
    timeout: float = 15.0,
    insecure: bool = False,
    use_proxy: bool = False,
    prefer_area_id: Optional[str] = None,
) -> int:
    """启动后台守护进程，返回进程PID。

    注意：此函数只启动进程，不等待其结束。
    """
    # 构建命令行参数
    cmd = [
        sys.executable, '-m', 'lcc',
        'pomo-daemon',
        '--duration', str(duration_seconds),
        '--low', str(low),
        '--high', str(high),
        '--cycles', str(cycles),
        '--interval', str(interval),
        '--timeout', str(timeout),
    ]

    if insecure:
        cmd.append('--insecure')
    if use_proxy:
        cmd.append('--proxy')
    if prefer_area_id:
        cmd.extend(['--prefer-area-id', str(prefer_area_id)])

    # 启动后台进程（分离）
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs['startupinfo'] = subprocess.STARTUPINFO()
        kwargs['startupinfo'].dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs['startupinfo'].wShowWindow = subprocess.SW_HIDE
    else:
        kwargs['start_new_session'] = True

    # 重定向输出到日志文件或空设备
    log_file = Path.home() / '.lcc' / 'pomo.log'
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, 'a') as f:
        f.write(f'--- 番茄钟守护进程启动于 {datetime.now().isoformat()} ---\n')
        f.write(f'命令: {" ".join(cmd)}\n')

    with open(log_file, 'a') as log_f:
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=log_f,
            **kwargs,
        )

    return proc.pid


def stop_daemon(pid: int) -> bool:
    """停止指定的守护进程。

    返回: True 如果成功发送信号，False 如果进程不存在
    """
    try:
        if sys.platform == 'win32':
            # Windows: 发送 CTRL_BREAK_EVENT
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GenerateConsoleCtrlEvent(1, pid)
        else:
            # Unix: 发送 SIGTERM
            os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError):
        # 进程已经不存在
        return False


def is_process_alive(pid: int) -> bool:
    """检查进程是否仍在运行。"""
    try:
        if sys.platform == 'win32':
            # Windows: 使用 OpenProcess 检查
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            # Unix: 发送信号 0
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, OSError, AttributeError):
        return False


def ensure_single_instance(lockfile: Optional[Path] = None) -> bool:
    """确保只有一个番茄钟实例在运行。

    返回: True 如果这是唯一实例，False 如果已有实例在运行
    """
    if lockfile is None:
        lockfile = Path.home() / '.lcc' / 'pomo.lock'

    lockfile.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 尝试创建锁文件（原子操作）
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)

        # 写入当前进程PID
        lockfile.write_text(str(os.getpid()))
        return True
    except FileExistsError:
        # 锁文件已存在，检查进程是否存活
        try:
            pid = int(lockfile.read_text().strip())
            if is_process_alive(pid):
                return False
            else:
                # 进程已死，清理锁文件并重新尝试
                lockfile.unlink(missing_ok=True)
                return ensure_single_instance(lockfile)
        except (ValueError, OSError):
            # 锁文件内容无效，清理并重新尝试
            lockfile.unlink(missing_ok=True)
            return ensure_single_instance(lockfile)
    except OSError:
        # 其他错误，假设可以继续
        return True


def cleanup_lockfile(lockfile: Optional[Path] = None) -> None:
    """清理锁文件（如果属于当前进程）。"""
    if lockfile is None:
        lockfile = Path.home() / '.lcc' / 'pomo.lock'

    try:
        if lockfile.exists():
            pid = int(lockfile.read_text().strip())
            if pid == os.getpid():
                lockfile.unlink(missing_ok=True)
    except (ValueError, OSError):
        lockfile.unlink(missing_ok=True)