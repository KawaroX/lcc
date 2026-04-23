from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable


_ROUTE_TIMEOUT_SEC = 2.0


@dataclass(frozen=True)
class RouteInfo:
    host: str
    interface: str
    detail: str = ""


def append_tun_route_hint(message: str, *, hosts: Iterable[str]) -> str:
    hint = tun_route_hint(hosts=hosts)
    if not hint:
        return message
    return f"{message}\n{hint}"


def tun_route_hint(*, hosts: Iterable[str]) -> str | None:
    for host in hosts:
        info = _route_info_for_host(host)
        if info is None:
            continue
        haystack = f"{info.interface} {info.detail}"
        if not _looks_like_tun_interface(haystack):
            continue
        detail = f"（{info.detail}）" if info.detail and info.detail != info.interface else ""
        return (
            f"检测到 {info.host} 当前路由可能走了 {info.interface}{detail}，"
            "这通常说明 TUN/VPN 接管了校园网域名。"
            "请把 *.buaa.edu.cn 加入 DIRECT/绕过规则，或临时关闭 TUN 后重试。"
        )
    return None


def _route_info_for_host(host: str) -> RouteInfo | None:
    host = (host or "").strip()
    if not host:
        return None
    system = platform.system().lower()
    if system == "darwin":
        return _darwin_route_info(host)
    if system == "linux":
        return _linux_route_info(host)
    if system == "windows":
        return _windows_route_info(host)
    return None


def _darwin_route_info(host: str) -> RouteInfo | None:
    output = _run(["route", "-n", "get", host])
    if not output:
        return None
    m = re.search(r"^\s*interface:\s*(\S+)\s*$", output, re.MULTILINE)
    if not m:
        return None
    return RouteInfo(host=host, interface=m.group(1))


def _linux_route_info(host: str) -> RouteInfo | None:
    output = _run(["ip", "route", "get", host])
    if not output:
        output = _run(["ip", "-6", "route", "get", host])
    if not output:
        return None
    m = re.search(r"\bdev\s+(\S+)", output)
    if not m:
        return None
    return RouteInfo(host=host, interface=m.group(1))


def _windows_route_info(host: str) -> RouteInfo | None:
    command = _windows_route_command(host)
    output = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command])
    if not output:
        output = _run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", command])
    if not output:
        return None

    alias = _parse_key_value(output, "InterfaceAlias")
    description = _parse_key_value(output, "InterfaceDescription")
    index = _parse_key_value(output, "InterfaceIndex")
    interface = alias or index
    if not interface:
        return None
    return RouteInfo(host=host, interface=interface, detail=description or interface)


def _windows_route_command(host: str) -> str:
    quoted = host.replace("'", "''")
    return (
        f"$target = '{quoted}'; "
        "$parsed = $null; "
        "if ([System.Net.IPAddress]::TryParse($target, [ref]$parsed)) { "
        "  $ip = $target "
        "} else { "
        "  $ip = Resolve-DnsName -Name $target -Type A -ErrorAction SilentlyContinue "
        "    | Select-Object -First 1 -ExpandProperty IPAddress "
        "} "
        "if ($ip) { "
        "  $r = Find-NetRoute -RemoteIPAddress $ip -ErrorAction SilentlyContinue "
        "    | Sort-Object RouteMetric, InterfaceMetric "
        "    | Select-Object -First 1; "
        "  if ($r) { "
        "    $a = Get-NetAdapter -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue; "
        "    if ($a) { "
        "      'InterfaceAlias=' + $a.Name; "
        "      'InterfaceDescription=' + $a.InterfaceDescription "
        "    } else { "
        "      'InterfaceIndex=' + $r.InterfaceIndex "
        "    } "
        "  } "
        "}"
    )


def _parse_key_value(output: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}=(.+)$", output, re.MULTILINE)
    return (m.group(1).strip() if m else "")


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_ROUTE_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _looks_like_tun_interface(value: str) -> bool:
    v = value.strip().lower()
    if not v:
        return False
    prefixes = ("utun", "tun", "tap", "ppp", "wg")
    if any(v.startswith(prefix) for prefix in prefixes):
        return True
    markers = (
        "wintun",
        "wireguard",
        "tailscale",
        "zerotier",
        "clash",
        "mihomo",
        "sing-box",
        "openvpn",
        "warp",
        "vpn",
    )
    return any(marker in v for marker in markers)
