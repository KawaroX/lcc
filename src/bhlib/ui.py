"""Tiny output toolkit for bhlib CLI.

Provides status primitives (ok/err/warn/info/tip), structured output helpers
(section/kv/table), and ANSI color wrappers. Automatically disables color
when the destination stream is not a TTY, or when NO_COLOR is set.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from typing import IO, Iterable, Sequence


_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _stream_supports_color(stream: IO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("BHLIB_FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _wrap(stream: IO, code: str, text: str) -> str:
    if not text or not _stream_supports_color(stream):
        return text
    return f"{code}{text}{_RESET}"


def green(s: str) -> str:
    return _wrap(sys.stdout, _GREEN, s)


def red(s: str) -> str:
    return _wrap(sys.stderr, _RED, s)


def yellow(s: str) -> str:
    return _wrap(sys.stdout, _YELLOW, s)


def cyan(s: str) -> str:
    return _wrap(sys.stdout, _CYAN, s)


def dim(s: str, *, stream: IO | None = None) -> str:
    return _wrap(stream or sys.stdout, _DIM, s)


def bold(s: str, *, stream: IO | None = None) -> str:
    return _wrap(stream or sys.stdout, _BOLD, s)


def _hint_lines(hint) -> list[str]:
    if hint is None:
        return []
    if isinstance(hint, str):
        return [line for line in hint.splitlines() if line.strip()]
    return [str(line) for line in hint if str(line).strip()]


def ok(msg: str, *, detail: str | None = None) -> None:
    sym = _wrap(sys.stdout, _GREEN, "✓")
    print(f"{sym} {msg}")
    if detail:
        print(_wrap(sys.stdout, _DIM, f"  · {detail}"))


def err(msg: str, *, hint=None) -> None:
    sym = _wrap(sys.stderr, _RED, "✗")
    print(f"{sym} {msg}", file=sys.stderr)
    for line in _hint_lines(hint):
        print(_wrap(sys.stderr, _DIM, f"  · {line}"), file=sys.stderr)


def warn(msg: str, *, hint=None) -> None:
    sym = _wrap(sys.stdout, _YELLOW, "!")
    print(f"{sym} {msg}")
    for line in _hint_lines(hint):
        print(_wrap(sys.stdout, _DIM, f"  · {line}"))


def info(msg: str) -> None:
    sym = _wrap(sys.stdout, _CYAN, "→")
    print(f"{sym} {msg}")


def tip(msg: str) -> None:
    print(_wrap(sys.stdout, _DIM, f"· {msg}"))


def section(title: str) -> None:
    print(_wrap(sys.stdout, _BOLD, title))


def kv(key: str, value: str, *, indent: int = 2, key_width: int | None = None) -> None:
    pad = " " * indent
    if key_width is None:
        label = key
    else:
        extra = max(0, key_width - _visible_width(key))
        label = key + (" " * extra)
    print(f"{pad}{_wrap(sys.stdout, _DIM, label + ':')} {value}")


def _visible_width(s: str) -> int:
    s = _ANSI_RE.sub("", s)
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("F", "W"):
            w += 2
        else:
            w += 1
    return w


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    aligns: Sequence[str] | None = None,
    indent: int = 2,
) -> None:
    cols = len(headers)
    aligns = list(aligns) if aligns else ["left"] * cols
    str_rows = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [_visible_width(h) for h in headers]
    for row in str_rows:
        for i in range(cols):
            if i < len(row):
                widths[i] = max(widths[i], _visible_width(row[i]))

    def fmt(text: str, w: int, align: str) -> str:
        pad = max(0, w - _visible_width(text))
        return (" " * pad + text) if align == "right" else (text + " " * pad)

    pad_indent = " " * indent
    print(pad_indent + "  ".join(
        _wrap(sys.stdout, _BOLD, fmt(h, widths[i], aligns[i]))
        for i, h in enumerate(headers)
    ))
    for row in str_rows:
        cells = [fmt(row[i] if i < len(row) else "", widths[i], aligns[i]) for i in range(cols)]
        print(pad_indent + "  ".join(cells))
