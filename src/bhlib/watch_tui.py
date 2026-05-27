"""bhlib watch notify --edit 的交互式编辑器。

↑↓ 选项 · ←→ 切换/调整 · space 翻转 · s 保存 · q 取消
- 通知开关：← = 关，→ = 开，space = 翻转
- 数字字段：← 减 1，→ 加 1（夹在 [0, 30]）

依赖 termios（macOS / Linux / iOS a-Shell 都支持）。Windows / 非 TTY
环境下返回 None，调用方提示用户走 flag 形式。
"""

from __future__ import annotations

import json
import os
import select
import sys
import unicodedata
from typing import Optional

from .config import WATCH_NOTIFY_TYPES


def _visual_width(s: str) -> int:
    """终端显示宽度（CJK 全角字符算 2 列）。"""
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _pad_visual(s: str, cols: int) -> str:
    pad = cols - _visual_width(s)
    return s + (" " * pad if pad > 0 else "")

_LABELS = {
    "new_free": "新增空位",
    "taken": "空位被占用",
    "temp_leave": "新临时离开",
    "expire_soon": "临时离开到期预警",
    "self_seat": "自己座位异常",
}
_HINTS = {
    "self_seat": "暂未实现",
}
_NUM_MIN = 0
_NUM_MAX = 30


def _build_items() -> list[dict]:
    items: list[dict] = [
        {"kind": "toggle", "key": k, "label": _LABELS.get(k, k), "hint": _HINTS.get(k, "")}
        for k in WATCH_NOTIFY_TYPES
    ]
    items.append(
        {
            "kind": "number",
            "key": "expire_warn_minutes",
            "label": "到期预警提前",
            "hint": f"{_NUM_MIN}-{_NUM_MAX} min",
        }
    )
    return items


def _render(cfg: dict, items: list[dict], cursor: int) -> list[str]:
    lines: list[str] = []
    lines.append("watch 通知设置")
    lines.append("")
    for i, it in enumerate(items):
        marker = "▸" if i == cursor else " "
        label = it["label"]
        if it["kind"] == "toggle":
            val = bool(cfg["notify"].get(it["key"], False))
            badge = "[ 开 ]" if val else "[ 关 ]"
        else:
            v = int(cfg.get(it["key"], 0))
            badge = f"<  {v:>2} min  >"
        hint = f"  ({it['hint']})" if it["hint"] else ""
        # 用东亚宽度计算填充，保证 badge 列对齐
        lines.append(f" {marker}  {_pad_visual(label, 20)}  {badge}{hint}")
    lines.append("")
    lines.append("  ↑↓ 选择 · ←→ 切换/调整 · space 翻转 · s 保存 · q 取消")
    return lines


def _read_key(fd: int) -> str:
    """返回单个动作 token：UP/DOWN/LEFT/RIGHT，或单字符。"""
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch != "\x1b":
        return ch
    # 可能是 ESC 序列；用 50ms 超时区分单独的 Esc
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return "\x1b"
    ch2 = os.read(fd, 1).decode(errors="ignore")
    if ch2 != "[":
        return "\x1b"
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return "\x1b"
    ch3 = os.read(fd, 1).decode(errors="ignore")
    return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3, "\x1b")


def edit_notify_config(cfg: dict) -> Optional[dict]:
    """打开 TUI 让用户编辑 cfg。返回新 cfg；用户取消时返回 None。

    cfg 不会被就地修改。
    """
    try:
        import termios
        import tty
    except ImportError:
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    # 深拷贝，避免就地修改
    cfg = json.loads(json.dumps(cfg))
    # 兜底字段
    cfg.setdefault("notify", {})
    for k in WATCH_NOTIFY_TYPES:
        cfg["notify"].setdefault(k, False)
    cfg.setdefault("expire_warn_minutes", 5)

    items = _build_items()
    cursor = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    saved: Optional[dict] = None
    # 进入备用屏幕缓冲、隐藏光标
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            sys.stdout.write("\x1b[H\x1b[J")  # home + clear-to-end
            for line in _render(cfg, items, cursor):
                sys.stdout.write(line + "\r\n")
            sys.stdout.flush()

            key = _read_key(fd)
            if key in ("q", "Q", "\x03"):
                saved = None
                break
            if key == "\x1b":
                saved = None
                break
            if key in ("s", "S", "\r", "\n"):
                saved = cfg
                break
            if key == "UP":
                cursor = (cursor - 1) % len(items)
                continue
            if key == "DOWN":
                cursor = (cursor + 1) % len(items)
                continue

            it = items[cursor]
            if it["kind"] == "toggle":
                if key == "LEFT":
                    cfg["notify"][it["key"]] = False
                elif key == "RIGHT":
                    cfg["notify"][it["key"]] = True
                elif key == " ":
                    cfg["notify"][it["key"]] = not bool(cfg["notify"].get(it["key"], False))
            elif it["kind"] == "number":
                cur_v = int(cfg.get(it["key"], 0))
                if key == "LEFT":
                    cfg[it["key"]] = max(_NUM_MIN, cur_v - 1)
                elif key == "RIGHT":
                    cfg[it["key"]] = min(_NUM_MAX, cur_v + 1)
    except KeyboardInterrupt:
        saved = None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # 还原屏幕 + 光标
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    return saved
