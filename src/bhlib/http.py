from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .ssl_ctx import make_ssl_context


class HttpError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    data: object


def _make_headers(*, token: str, cookie: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://booking.lib.buaa.edu.cn",
        "Referer": "https://booking.lib.buaa.edu.cn/h5/index.html",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "authorization": f"bearer{token}",
        "Cookie": cookie,
        "Connection": "keep-alive",
    }


def _build_opener(*, ctx, use_proxy: bool) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPSHandler(context=ctx)]
    if not use_proxy:
        # ProxyHandler({}) disables both env-var proxies and macOS system proxies.
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def post_json(
    *,
    base_url: str,
    path: str,
    token: str,
    cookie: str,
    json_body: object,
    timeout_sec: float = 15.0,
    verify_ssl: bool = True,
    use_proxy: bool = False,
) -> object:
    url = base_url.rstrip("/") + path
    body_bytes = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        method="POST",
        data=body_bytes,
        headers=_make_headers(token=token, cookie=cookie),
    )

    ctx = make_ssl_context(verify_ssl=verify_ssl)
    opener = _build_opener(ctx=ctx, use_proxy=use_proxy)
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise HttpError(f"返回不是 JSON（HTTP {resp.status}）: {raw[:200]}") from e
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise HttpError(f"HTTP {e.code}: {raw[:200]}") from e
    except urllib.error.URLError as e:
        raise HttpError(f"网络错误: {e}") from e
