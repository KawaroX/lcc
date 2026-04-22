from __future__ import annotations

import os
import struct
import tempfile
import unicodedata
import zlib
from collections import Counter, defaultdict
from typing import NamedTuple

# True-color (RGB) background codes — muted pastel palette.
_STATUS_COLOR = {
    "1": (132, 165, 157),   # 空闲   -> #84A59D 灰绿
    "6": (242, 132, 130),   # 使用中 -> #F28482 珊瑚粉
    "7": (247, 237, 226),   # 临时离开 -> #F7EDE2 奶油
    "2": (245, 202, 195),   # 已预约 -> #F5CAC3 浅桃
}
_DEFAULT_COLOR = (146, 146, 146)  # 默认浅灰

_STATUS_NAME = {
    "1": "空闲",
    "6": "使用中",
    "7": "临时离开",
    "2": "已预约",
}

CELL_W = 3
STRIDE_X = CELL_W + 1

X_CLUSTER = 0.6
Y_CLUSTER = 0.5

# Y-gap between consecutive y-clusters. Below this we treat them as a "glued
# pair" (no blank line between); at or above this they are separate (blank
# line between). Tuned for this library: within-pair ~3.5, between-group >=5.
GLUE_Y_GAP = 4.5

# Horizontal separator between regions in the composed output.
REGION_SEP = "    "


class _RegionGrid(NamedTuple):
    rows: list[tuple[list[str], list[int | None]] | None]
    width: int
    x_shift: int
    y_shift: int


def _ansi_bg(code: tuple[int, int, int] | int) -> str:
    # Support both RGB tuples (true-color) and legacy int codes.
    if isinstance(code, tuple):
        r, g, b = code
        return f"48;2;{r};{g};{b}"
    return f"48;5;{code}"


def _fnum(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _seat_label(no: str, cell_w: int) -> str:
    n = (no or "").strip()
    if not n:
        return " " * cell_w
    if n.isdigit():
        nn = n.lstrip("0") or "0"
        return (nn[-cell_w:]).rjust(cell_w)
    return (n[:cell_w]).ljust(cell_w)


def _cluster(values: list[float], threshold: float) -> tuple[dict[float, int], list[float]]:
    """Greedy 1-D clustering. Returns (index_of_value, cluster_centers)."""
    if not values:
        return {}, []
    uniq = sorted(set(values))
    index_of: dict[float, int] = {}
    groups: list[list[float]] = [[uniq[0]]]
    index_of[uniq[0]] = 0
    for v in uniq[1:]:
        if v - groups[-1][-1] < threshold:
            groups[-1].append(v)
            index_of[v] = len(groups) - 1
        else:
            groups.append([v])
            index_of[v] = len(groups) - 1
    centers = [sum(g) / len(g) for g in groups]
    return index_of, centers


def _region_of(no_int: int) -> str | None:
    if 1 <= no_int <= 26:
        return "A"
    if 27 <= no_int <= 72:
        return "B"
    if 73 <= no_int <= 100:
        return "C"
    if 101 <= no_int <= 175:
        return "D"
    return None


def _render_row(color_row: list, char_row: list, cols: int) -> str:
    reset = "\x1b[0m"
    buf: list[str] = []
    last_color: int | None = -1  # type: ignore[assignment]
    for c in range(cols):
        color = color_row[c]
        ch = char_row[c]
        if color is None:
            if last_color is not None:
                buf.append(reset)
                last_color = None
            buf.append(ch)
        else:
            if color != last_color:
                buf.append(f"\x1b[30;{_ansi_bg(color)}m")
                last_color = color
            buf.append(ch)
    if last_color is not None:
        buf.append(reset)
    return "".join(buf).rstrip()


def _visible_width(s: str) -> int:
    out = 0
    i = 0
    in_esc = False
    while i < len(s):
        ch = s[i]
        if ch == "\x1b":
            in_esc = True
            i += 1
            continue
        if in_esc:
            if ch == "m":
                in_esc = False
            i += 1
            continue
        out += 1
        i += 1
    return out


def _terminal_width(s: str) -> int:
    """Visible width counting CJK chars as 2 cols; skips ANSI escape sequences."""
    out = 0
    i = 0
    in_esc = False
    while i < len(s):
        ch = s[i]
        if ch == "\x1b":
            in_esc = True
            i += 1
            continue
        if in_esc:
            if ch == "m":
                in_esc = False
            i += 1
            continue
        out += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        i += 1
    return out


def _pad_visible(s: str, width: int) -> str:
    pad = width - _visible_width(s)
    if pad > 0:
        return s + " " * pad
    return s


def _render_region(
    seats: list[tuple[int, float, float, str]],
    counts: Counter,
    region_x_idx: dict[float, int] | None = None,
    *,
    region_cols: int | None = None,
    region_right_align: bool = False,
) -> tuple[list[tuple[list[str], list[int | None]] | None], int]:
    """Render a region into rows of (char_row, color_row). Returns (rows, width).

    Algorithm: y-cluster -> form glued-groups (consecutive clusters with gap
    < GLUE_Y_GAP). Each group x-clusters independently and flattens seats
    into collision-bumped stacks, so rows with complementary x (e.g.
    165-175 bottom row split across two y values) merge into one line.

    When *region_x_idx* is provided, all groups in the region share the same
    x-coordinate mapping. This is useful for regions like C and D where
    different glued groups occupy the same physical x-columns and should
    align vertically.
    """
    if not seats:
        return [], 0

    ys = [s[2] for s in seats]
    y_idx, y_centers = _cluster(ys, Y_CLUSTER)

    by_y: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
    for no, x, y, status in seats:
        by_y[y_idx[y]].append((no, x, status))

    y_order = sorted(by_y.keys(), key=lambda k: y_centers[k])

    # Partition y-clusters into glued groups. A cluster joins the current
    # group when gap < GLUE_Y_GAP. Additionally, once the current group
    # already holds 2 multi-seat clusters (one glued table pair), a new
    # multi-seat cluster starts a fresh group — this separates the 165-175
    # bottom strip from the 153-164 pair. Single-seat clusters (e.g. the
    # 1-26 column) don't count toward the cap, so the left column stays
    # tight without artificial breaks.
    def _is_multi(yk: int) -> bool:
        return len(by_y[yk]) >= 2

    groups: list[list[int]] = [[y_order[0]]]
    for i in range(1, len(y_order)):
        gap = y_centers[y_order[i]] - y_centers[y_order[i - 1]]
        yk = y_order[i]
        multi_in_group = sum(1 for k in groups[-1] if _is_multi(k))
        if gap >= GLUE_Y_GAP:
            groups.append([yk])
        elif _is_multi(yk) and multi_in_group >= 2:
            groups.append([yk])
        else:
            groups[-1].append(yk)

    # Fixed width for region-wide mode (optional).
    fixed_cols = region_cols if (region_x_idx is not None and region_cols is not None) else None

    rows: list[tuple[list[str], list[int | None]] | None] = []
    for gi, group in enumerate(groups):
        if gi > 0:
            rows.append(None)

        group_seats: list[tuple[int, float, str]] = []
        for yk in group:
            group_seats.extend(by_y[yk])

        if region_x_idx is not None:
            x_idx = region_x_idx
            if fixed_cols is not None:
                cols = fixed_cols
            else:
                num_gx = (max(x_idx.values()) + 1) if x_idx else 1
                cols = num_gx * STRIDE_X
        else:
            group_xs = [s[1] for s in group_seats]
            x_idx, _ = _cluster(group_xs, X_CLUSTER)
            num_gx = (max(x_idx.values()) + 1) if x_idx else 1
            cols = num_gx * STRIDE_X

        # Each y-cluster becomes its own stack, in y order. This preserves
        # the row structure (top row vs bottom row of a glued pair).
        stacks: list[list[tuple[int, int, str]]] = []
        for yk in group:
            stack: list[tuple[int, int, str]] = []
            for no, x, status in sorted(by_y[yk], key=lambda s: x_idx[s[1]]):
                gx = x_idx[x]
                if any(other_gx == gx for other_gx, _, _ in stack):
                    stacks.append(stack)
                    stack = []
                stack.append((gx, no, status))
            if stack:
                stacks.append(stack)

        # Merge adjacent stacks if their x-columns are fully disjoint, so a
        # row split only by y-jitter (e.g. 167-168 at y=89 vs the rest of
        # 165-175 at y=93) renders as one line.
        merged = True
        while merged and len(stacks) >= 2:
            merged = False
            for i in range(len(stacks) - 1):
                cols_a = {gx for gx, _, _ in stacks[i]}
                cols_b = {gx for gx, _, _ in stacks[i + 1]}
                if cols_a.isdisjoint(cols_b):
                    stacks[i] = sorted(stacks[i] + stacks[i + 1], key=lambda c: c[0])
                    del stacks[i + 1]
                    merged = True
                    break

        for stack in stacks:
            char_row = [" "] * cols
            color_row: list[int | None] = [None] * cols
            max_gx = max(gx for gx, _, _ in stack) if stack else 0
            start_offset = 0
            if region_x_idx is not None and region_right_align:
                row_right = max_gx * STRIDE_X + CELL_W
                indent = cols - row_right
                start_offset = max(0, indent)
            for gx, no, status in stack:
                color = _STATUS_COLOR.get(status, _DEFAULT_COLOR)
                label = _seat_label(str(no).zfill(3), CELL_W)
                start = start_offset + gx * STRIDE_X
                for i, ch in enumerate(label):
                    cx = start + i
                    if 0 <= cx < cols:
                        char_row[cx] = ch
                        color_row[cx] = color
                counts[status] += 1
            rows.append((char_row, color_row))

    def _row_used_width(rr: tuple[list[str], list[int | None]]) -> int:
        chars, colors = rr
        for i in range(len(chars) - 1, -1, -1):
            if colors[i] is not None or chars[i] != " ":
                return i + 1
        return 0

    width = max((_row_used_width(r) for r in rows if r is not None), default=0)
    if width > 0:
        for i, rr in enumerate(rows):
            if rr is None:
                continue
            chars, colors = rr
            if len(chars) > width:
                rows[i] = (chars[:width], colors[:width])
    return rows, width


def render_seat_map(
    seats: list[dict],
    *,
    compress_blank_rows: bool = True,
    status_names: dict[str, str] | None = None,
) -> str:
    if not seats:
        return "(no seats)"

    area_name = ""
    for s in seats:
        n = str(s.get("area_name") or "").strip()
        if n:
            area_name = n
            break

    geoms: list[tuple[int, float, float, str]] = []
    misfits: list[tuple[int, float, float, str]] = []
    for s in seats:
        no_raw = str(s.get("no") or "").strip()
        try:
            no_int = int(no_raw)
        except ValueError:
            continue
        x = _fnum(s.get("point_x"))
        y = _fnum(s.get("point_y"))
        status = str(s.get("status") or "")
        row = (no_int, x, y, status)
        if _region_of(no_int) is None:
            misfits.append(row)
        else:
            geoms.append(row)

    if not geoms and not misfits:
        return "(no seats with geometry)"

    regions: dict[str, list[tuple[int, float, float, str]]] = {
        "A": [],
        "B": [],
        "C": [],
        "D": [],
    }
    for g in geoms:
        regions[_region_of(g[0])].append(g)  # type: ignore[index]

    counts: Counter = Counter()
    grids: dict[str, _RegionGrid] = {}

    # Region A first — its height anchors the bottom alignment for B and D.
    a_rows, a_width = _render_region(regions["A"], counts, region_x_idx=None)
    grids["A"] = _RegionGrid(rows=a_rows, width=a_width, x_shift=0, y_shift=0)
    a_height = len(a_rows)

    # Region D: shared x-cluster + right-align (175 under 164), and shift the
    # whole region left by the empty leading columns before seat 101 so that
    # "76    101" spacing stays normal. Then push it down so its bottom row
    # lands on A's last row.
    if regions["D"]:
        xs = [g[1] for g in regions["D"]]
        region_x_idx, _ = _cluster(xs, X_CLUSTER)

        main_gxs: list[int] = []
        for no, x, _y, _st in regions["D"]:
            if 101 <= no <= 164:
                gx = region_x_idx.get(x)
                if gx is not None:
                    main_gxs.append(gx)
        if main_gxs:
            anchor_gx = min(main_gxs)
            target_max_gx = max(main_gxs)
            d_cols = target_max_gx * STRIDE_X + CELL_W
            d_x_shift = -(anchor_gx * STRIDE_X)
            d_rows, d_width = _render_region(
                regions["D"],
                counts,
                region_x_idx=region_x_idx,
                region_cols=d_cols,
                region_right_align=True,
            )
        else:
            d_rows, d_width = _render_region(regions["D"], counts, region_x_idx=None)
            d_x_shift = 0
    else:
        d_rows, d_width, d_x_shift = [], 0, 0

    d_y_shift = max(0, a_height - len(d_rows)) if d_rows else 0
    grids["D"] = _RegionGrid(rows=d_rows, width=d_width, x_shift=d_x_shift, y_shift=d_y_shift)

    # B/C share D's baseline shift, plus 2 to align "31.."/"73.." with D's
    # "107.." row (D's first visible row "101..106" sits one separator above).
    bc_y_shift = d_y_shift + 2

    # Region B: render with per-group x-clustering. If the detached "27-30"
    # row is present, pad before it so it lands on A's last row (alongside
    # D's bottom row 165..175).
    b_rows, b_width = _render_region(regions["B"], counts, region_x_idx=None)
    if (
        b_rows
        and a_height > 0
        and any(s[0] in (27, 28, 29, 30) for s in regions["B"])
    ):
        last_idx = next(
            (i for i in range(len(b_rows) - 1, -1, -1) if b_rows[i] is not None),
            None,
        )
        if last_idx is not None:
            pad = (a_height - 1) - (bc_y_shift + last_idx)
            if pad > 0:
                b_rows = b_rows[:last_idx] + [None] * pad + b_rows[last_idx:]
    grids["B"] = _RegionGrid(rows=b_rows, width=b_width, x_shift=0, y_shift=bc_y_shift)

    # Region C: same baseline as B.
    c_rows, c_width = _render_region(regions["C"], counts, region_x_idx=None)
    grids["C"] = _RegionGrid(rows=c_rows, width=c_width, x_shift=0, y_shift=bc_y_shift)

    active = [name for name in ("A", "B", "C", "D") if grids[name].rows]
    if not active:
        return "(no seats with geometry)"

    height = max((len(grids[n].rows) + grids[n].y_shift) for n in active)
    composed: list[str] = []

    # Compute base x offsets for each region (without shifts).
    offsets: dict[str, int] = {}
    x = 0
    for i, n in enumerate(active):
        offsets[n] = x
        x += grids[n].width
        if i != len(active) - 1:
            x += len(REGION_SEP)
    base_total_cols = x

    # If shifts push content left of 0 (e.g. only Region D active), add a left pad.
    min_col = 0
    for n in active:
        min_col = min(min_col, offsets[n] + grids[n].x_shift)
    left_pad = -min_col if min_col < 0 else 0
    total_cols = base_total_cols + left_pad

    # Visible right edge of the rendered map: when D is shifted left by its
    # leading empty columns, the trailing empty columns inside base_total_cols
    # never get content. Use the actual extent for centering the legend.
    visible_right = max(
        (offsets[n] + grids[n].x_shift + grids[n].width for n in active),
        default=total_cols,
    ) + left_pad

    for r in range(height):
        char_row = [" "] * total_cols
        color_row: list[int | None] = [None] * total_cols

        any_content = False
        for n in active:
            grid = grids[n]
            rr_idx = r - grid.y_shift
            rr = grid.rows[rr_idx] if 0 <= rr_idx < len(grid.rows) else None
            if rr is None:
                continue
            src_chars, src_colors = rr
            base = offsets[n] + grid.x_shift + left_pad
            for i in range(min(len(src_chars), len(src_colors))):
                ch = src_chars[i]
                col = base + i
                if col < 0 or col >= total_cols:
                    continue
                color = src_colors[i]
                if color is None and ch == " ":
                    continue
                if color_row[col] is None and char_row[col] == " ":
                    char_row[col] = ch
                    color_row[col] = color
                    any_content = True
        if not any_content:
            composed.append("")
        else:
            composed.append(_render_row(color_row, char_row, total_cols))

    # Any stray seats not matched by region rules: append as extra lines below.
    if misfits:
        extra_rows, extra_w = _render_region(misfits, counts)
        if extra_rows:
            composed.append("")
            for rr in extra_rows:
                if rr is None:
                    composed.append("")
                    continue
                chars, colors = rr
                if not any(c is not None for c in colors) and not any(ch != " " for ch in chars):
                    composed.append("")
                else:
                    composed.append(_render_row(colors, chars, len(chars)))

    reset = "\x1b[0m"
    names = status_names if status_names is not None else _STATUS_NAME
    legend_specs: list[tuple[int, str]] = []
    for st in ("1", "2", "6", "7"):
        cnt = counts.get(st, 0)
        if cnt <= 0:
            continue
        color = _STATUS_COLOR.get(st, _DEFAULT_COLOR)
        name = names.get(st, f"status={st}")
        legend_specs.append((color, f"{name} × {cnt}"))
    for st, cnt in counts.items():
        if st in ("1", "2", "6", "7") or cnt <= 0:
            continue
        color = _STATUS_COLOR.get(st, _DEFAULT_COLOR)
        name = names.get(st, f"status={st}")
        legend_specs.append((color, f"{name} × {cnt}"))

    # Try to inject the legend as a 2-row block (color blocks above labels),
    # horizontally centered within the empty area to the right of A.
    injected = False
    if legend_specs and a_height > 5:
        item_widths = [max(3, _terminal_width(label)) for _, label in legend_specs]
        spacing = 3
        block_w = sum(item_widths) + spacing * (len(legend_specs) - 1)
        left_bound = a_width + len(REGION_SEP)
        avail = visible_right - left_bound
        top_idx = (8 - 2) // 2  # vertically centered in the 8-row top band
        if avail >= block_w and top_idx + 1 < len(composed):
            indent = left_bound + (avail - block_w) // 2
            blocks_parts: list[str] = []
            labels_parts: list[str] = []
            for i, (color, label) in enumerate(legend_specs):
                item_w = item_widths[i]
                block = f"\x1b[30;{_ansi_bg(color)}m   {reset}"
                bp_l = (item_w - 3) // 2
                blocks_parts.append(" " * bp_l + block + " " * (item_w - 3 - bp_l))
                lw = _terminal_width(label)
                lp_l = (item_w - lw) // 2
                labels_parts.append(" " * lp_l + label + " " * (item_w - lw - lp_l))
            sep = " " * spacing
            row_blocks = sep.join(blocks_parts)
            row_labels = sep.join(labels_parts)
            top_existing = composed[top_idx]
            bot_existing = composed[top_idx + 1]
            if (
                _terminal_width(top_existing) <= indent
                and _terminal_width(bot_existing) <= indent
            ):
                composed[top_idx] = (
                    top_existing
                    + " " * (indent - _terminal_width(top_existing))
                    + row_blocks
                )
                composed[top_idx + 1] = (
                    bot_existing
                    + " " * (indent - _terminal_width(bot_existing))
                    + row_labels
                )
                injected = True

    title = area_name if area_name else "座位图"
    parts = [title] + composed
    if legend_specs and not injected:
        legend_line = "  ".join(
            f"\x1b[30;{_ansi_bg(c)}m   {reset} {l}" for c, l in legend_specs
        )
        parts.append("")
        parts.append(legend_line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Seat-map image export (pure Python, zero external dependencies)
# ---------------------------------------------------------------------------

_CHAR_W = 16
_CHAR_H = 24
_FONT_SCALE = 2

# 8×8 bitmap font for digits 0-9 (classic).  Rendered at 2× → 16×16.
_FONT_8x8: dict[str, list[int]] = {
    "0": [0x3C, 0x66, 0x6E, 0x76, 0x66, 0x66, 0x3C, 0x00],
    "1": [0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00],
    "2": [0x3C, 0x66, 0x06, 0x0C, 0x30, 0x60, 0x7E, 0x00],
    "3": [0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00],
    "4": [0x06, 0x0E, 0x1E, 0x66, 0x7F, 0x06, 0x06, 0x00],
    "5": [0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00],
    "6": [0x3C, 0x66, 0x60, 0x7C, 0x66, 0x66, 0x3C, 0x00],
    "7": [0x7E, 0x06, 0x0C, 0x18, 0x30, 0x30, 0x30, 0x00],
    "8": [0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00],
    "9": [0x3C, 0x66, 0x66, 0x3E, 0x06, 0x66, 0x3C, 0x00],
}


def _parse_ansi_line(line: str) -> list[tuple[str, tuple[int, int, int] | None, int]]:
    """Parse a single ANSI-coloured line into (char, bg_color, width)."""
    cells: list[tuple[str, tuple[int, int, int] | None, int]] = []
    i = 0
    bg: tuple[int, int, int] | None = None
    while i < len(line):
        if line.startswith("\x1b[0m", i):
            bg = None
            i += 4
            continue
        if line.startswith("\x1b[30;48;2;", i):
            end = line.find("m", i)
            if end != -1:
                codes = line[i + 2 : end].split(";")
                if len(codes) >= 6 and codes[1] == "48" and codes[2] == "2":
                    bg = (int(codes[3]), int(codes[4]), int(codes[5]))
                i = end + 1
                continue
            i += 1
            continue
        ch = line[i]
        w = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        cells.append((ch, bg, w))
        i += 1
    # strip trailing plain-space cells so they don't inflate image width
    while cells and cells[-1][0] == " " and cells[-1][1] is None:
        cells.pop()
    return cells


def _write_png(width: int, height: int, pixels: bytes) -> bytes:
    """Write an RGB PNG using only stdlib zlib/struct."""

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(data, zlib.crc32(ctype)) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    stride = width * 3
    for y in range(height):
        raw += b"\x00" + pixels[y * stride : (y + 1) * stride]
    compressed = zlib.compress(raw)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


def _draw_rect(
    px: bytearray,
    img_w: int,
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple[int, int, int],
) -> None:
    r, g, b = color
    y_start = max(0, y)
    y_end = min(len(px) // (img_w * 3), y + h)
    x_start = max(0, x)
    x_end = min(img_w, x + w)
    for yy in range(y_start, y_end):
        offset = (yy * img_w + x_start) * 3
        for _ in range(x_end - x_start):
            px[offset] = r
            px[offset + 1] = g
            px[offset + 2] = b
            offset += 3


def _draw_char(
    px: bytearray,
    img_w: int,
    x: int,
    y: int,
    char: str,
    color: tuple[int, int, int] = (0, 0, 0),
) -> None:
    bits = _FONT_8x8.get(char)
    if not bits:
        return
    r, g, b = color
    for row_idx, row_bits in enumerate(bits):
        for col_idx in range(8):
            if row_bits & (1 << (7 - col_idx)):
                for dy in range(_FONT_SCALE):
                    for dx in range(_FONT_SCALE):
                        cx = x + col_idx * _FONT_SCALE + dx
                        cy = y + row_idx * _FONT_SCALE + dy
                        if 0 <= cx < img_w and 0 <= cy < len(px) // (img_w * 3):
                            off = (cy * img_w + cx) * 3
                            px[off] = r
                            px[off + 1] = g
                            px[off + 2] = b


_STATUS_NAME_EN: dict[str, str] = {
    "1": "Free",
    "6": "In Use",
    "7": "Away",
    "2": "Reserved",
}


def render_seat_map_to_image_bytes(seats: list[dict]) -> bytes:
    """Render a seat map as a PNG image (bytes).  Pure Python, no external deps."""
    ansi = render_seat_map(seats, status_names=_STATUS_NAME_EN)
    lines = ansi.splitlines()
    grids = [_parse_ansi_line(line) for line in lines]

    rows = len(grids)
    cols = 0
    for row in grids:
        row_width = sum(w for _ch, _bg, w in row)
        cols = max(cols, row_width)

    img_w = cols * _CHAR_W
    img_h = rows * _CHAR_H
    if img_w <= 0 or img_h <= 0:
        img_w = img_h = 1

    px = bytearray(img_w * img_h * 3)
    # white background
    for i in range(0, len(px), 3):
        px[i] = px[i + 1] = px[i + 2] = 255

    for row_idx, row in enumerate(grids):
        x = 0
        y = row_idx * _CHAR_H
        for ch, bg, w in row:
            cell_w = w * _CHAR_W
            if bg is not None:
                _draw_rect(px, img_w, x, y, cell_w, _CHAR_H, bg)
            if ch in _FONT_8x8:
                fx = x + (cell_w - 8 * _FONT_SCALE) // 2
                fy = y + (_CHAR_H - 8 * _FONT_SCALE) // 2
                _draw_char(px, img_w, fx, fy, ch)
            x += cell_w

    return _write_png(img_w, img_h, bytes(px))


def render_seat_map_to_image(seats: list[dict], path: str | None = None) -> str:
    """Render seat map to a PNG file and return the absolute path.

    *path* – destination file path.  When ``None`` a temp file under
    :func:`tempfile.gettempdir()` is used.
    """
    png_bytes = render_seat_map_to_image_bytes(seats)
    if path is None:
        fd, path = tempfile.mkstemp(prefix="bhlib_seatmap_", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
    else:
        path = os.path.abspath(path)
        with open(path, "wb") as f:
            f.write(png_bytes)
    return path
