from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar

from .netdiag import append_tun_route_hint
from .ssl_ctx import make_ssl_context


class CasLoginError(RuntimeError):
    pass


@dataclass(frozen=True)
class CasLoginResult:
    token: str
    cookie: str


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.locations: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if newurl:
            self.locations.append(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _extract_execution(html: str) -> str | None:
    m = re.search(r'name="execution"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r"name='execution'\s+value='([^']+)'", html)
    if m:
        return m.group(1)
    return None


def _extract_cas_from_urls(urls: list[str]) -> str | None:
    for url in urls:
        m = re.search(r"(?:\?|&)cas=([a-fA-F0-9]+)", url)
        if m:
            return m.group(1)
        m = re.search(r"cas=([a-fA-F0-9]+)", url)
        if m:
            return m.group(1)
    return None


def _cookie_header_for_domain(jar: CookieJar, *, domain_contains: str) -> str:
    parts: list[str] = []
    for c in jar:
        domain = (c.domain or "").lstrip(".")
        if domain_contains not in domain:
            continue
        parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def _seed_cookie_from_header(jar: CookieJar, *, cookie_header: str, domain: str) -> None:
    cookie_header = (cookie_header or "").strip()
    if not cookie_header:
        return
    pairs = [p.strip() for p in cookie_header.split(";") if p.strip()]
    for pair in pairs:
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )


def cas_login(
    *,
    username: str,
    password: str,
    service_url: str = "https://booking.lib.buaa.edu.cn/v4/login/cas",
    sso_login_base: str = "https://sso.buaa.edu.cn/login",
    initial_booking_cookie: str | None = None,
    user_agent: str | None = None,
    timeout_sec: float = 20.0,
    verify_ssl: bool = True,
    use_proxy: bool = False,
) -> CasLoginResult:
    """
    CAS 登录并换取 booking.lib.buaa.edu.cn 的 JWT token。

    仅模拟浏览器的正常登录跳转流程；不会保存账号密码。
    """
    username = (username or "").strip()
    password = password or ""
    if not username:
        raise CasLoginError("username 为空")
    if not password:
        raise CasLoginError("password 为空")

    ua = user_agent or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )

    ctx = make_ssl_context(verify_ssl=verify_ssl)
    jar = CookieJar()
    redirect = _RedirectRecorder()
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPCookieProcessor(jar),
        redirect,
        urllib.request.HTTPSHandler(context=ctx),
    ]
    if not use_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    service_param = urllib.parse.quote(service_url, safe="")
    login_url = f"{sso_login_base}?service={service_param}"

    # Optional: seed booking cookies like _zte_cid_ (helps some deployments).
    if initial_booking_cookie:
        # We only seed booking domain; SSO cookies are acquired via GET below.
        _seed_cookie_from_header(jar, cookie_header=initial_booking_cookie, domain="booking.lib.buaa.edu.cn")

    # Step 1: GET login page to extract execution.
    req_get = urllib.request.Request(
        url=login_url,
        method="GET",
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with opener.open(req_get, timeout=timeout_sec) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        msg = append_tun_route_hint(f"获取 SSO 登录页失败: {e}", hosts=["sso.buaa.edu.cn"])
        raise CasLoginError(msg) from e

    execution = _extract_execution(html)
    if not execution:
        raise CasLoginError("无法从 SSO 登录页提取 execution（页面结构可能变了）")

    # Step 2: POST credentials (follow redirects).
    form = {
        "username": username,
        "password": password,
        "submit": "LOGIN",
        "type": "username_password",
        "execution": execution,
        "_eventId": "submit",
    }
    body = urllib.parse.urlencode(form).encode("utf-8")
    req_post = urllib.request.Request(
        url=login_url,
        method="POST",
        data=body,
        headers={
            "User-Agent": ua,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://sso.buaa.edu.cn",
            "Referer": login_url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with opener.open(req_post, timeout=timeout_sec) as resp:
            final_url = resp.geturl()
            _ = resp.read()  # drain
    except urllib.error.HTTPError as e:
        raise CasLoginError(f"SSO 登录失败（HTTP {e.code}）") from e
    except urllib.error.URLError as e:
        msg = append_tun_route_hint(f"SSO 登录请求失败: {e}", hosts=["sso.buaa.edu.cn"])
        raise CasLoginError(msg) from e

    cas = _extract_cas_from_urls([final_url, *redirect.locations])
    if not cas:
        raise CasLoginError("未能在重定向链路中找到 cas 参数（可能账号/密码错误或流程变更）")

    # Step 3: exchange cas for JWT token.
    token_url = "https://booking.lib.buaa.edu.cn/v4/login/user"
    token_body = json.dumps({"cas": cas}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req_token = urllib.request.Request(
        url=token_url,
        method="POST",
        data=token_body,
        headers={
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://booking.lib.buaa.edu.cn",
            "Referer": "https://booking.lib.buaa.edu.cn/h5/index.html",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with opener.open(req_token, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise CasLoginError(f"换取 token 失败（HTTP {e.code}）: {raw[:200]}") from e
    except urllib.error.URLError as e:
        msg = append_tun_route_hint(f"换取 token 网络错误: {e}", hosts=["booking.lib.buaa.edu.cn"])
        raise CasLoginError(msg) from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CasLoginError(f"换取 token 返回不是 JSON: {raw[:200]}") from e

    if data.get("code") != 0:
        msg = data.get("message") or data.get("msg") or str(data)
        raise CasLoginError(f"换取 token 失败: {msg}")

    member = ((data.get("data") or {}).get("member") or {})
    token = (member.get("token") or "").strip()
    if not token:
        raise CasLoginError("响应里没有找到 token")

    booking_cookie = _cookie_header_for_domain(jar, domain_contains="booking.lib.buaa.edu.cn")
    if not booking_cookie:
        raise CasLoginError("未获取到 booking 域的 cookie（PHPSESSID 可能缺失）")

    return CasLoginResult(token=token, cookie=booking_cookie)
