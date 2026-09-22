"""Security boundary for the Web UI; independent of downloader credentials."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import os
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx
import anyio
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


class WebAuth:
    """HTTP Basic auth covers HTML, static assets, docs and all API routes."""

    def __init__(self):
        username = os.environ.get("XHS_WEB_USERNAME", "admin")
        password = os.environ.get("XHS_WEB_PASSWORD", "")
        if not username or ":" in username or len(password) < 16:
            raise RuntimeError(
                "请设置 XHS_WEB_USERNAME（默认 admin）及至少 16 字符的 "
                "XHS_WEB_PASSWORD，Web 服务不会在未配置认证时启动"
            )
        self.digest = hashlib.sha256(f"{username}:{password}".encode()).digest()

    async def __call__(self, request: Request, call_next):
        try:
            scheme, token = request.headers.get("authorization", "").split(" ", 1)
            decoded = base64.b64decode(token, validate=True)
            authenticated = scheme.lower() == "basic" and hmac.compare_digest(
                hashlib.sha256(decoded).digest(), self.digest
            )
        except (ValueError, binascii.Error):
            authenticated = False
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }
        if not authenticated:
            return JSONResponse(
                {"ok": False, "message": "请使用管理账号登录"}, status_code=401,
                headers=headers | {"WWW-Authenticate": 'Basic realm="XHS Web", charset="UTF-8"'},
            )
        # Basic credentials are browser-managed: protect against ambient-credential CSRF.
        # Custom header is deliberately non-simple, so foreign pages require a CORS preflight.
        if (request.headers.get("sec-fetch-site") == "cross-site" or
                (request.method not in {"GET", "HEAD", "OPTIONS"} and
                 request.headers.get("x-xhs-request") != "1")):
            return JSONResponse(
                {"ok": False, "message": "请求来源校验失败"}, status_code=403, headers=headers,
            )
        response = await call_next(request)
        response.headers.update(headers)
        return response


def public_settings(settings: dict) -> dict:
    return {k: v for k, v in settings.items() if k != "cookie"} | {
        "cookie_configured": bool(settings.get("cookie")),
    }


def validate_media_url(value: str) -> httpx.URL:
    try:
        if len(value) > 8192 or any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme not in {"http", "https"} or parts.username is not None or
                parts.password is not None or parts.fragment or
                not (host == "ci.xiaohongshu.com" or
                     re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+xhscdn\.com", host)) or
                parts.port not in {None, 443 if parts.scheme == "https" else 80}):
            raise ValueError
        return httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise HTTPException(400, "仅允许小红书 CDN 的标准 HTTP/HTTPS 媒体地址") from None


async def resolve_public_ip(host: str, port: int) -> str:
    try:
        answers = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=10,
        )
    except (OSError, TimeoutError):
        raise HTTPException(502, "媒体域名解析失败") from None
    addresses = [ipaddress.ip_address(item[4][0]) for item in answers]
    if not addresses:
        raise HTTPException(502, "媒体域名没有可用地址")
    for address in addresses:
        if (not address.is_global or address.is_multicast or address.is_reserved or
                (address.version == 6 and (
                    address.ipv4_mapped or address.sixtofour or address.teredo or
                    address in ipaddress.ip_network("64:ff9b::/96")))):
            raise HTTPException(400, "媒体目标地址不允许访问")
    # Prefer IPv4 where available, but never resolve this hostname again at connect time.
    return str(next((a for a in addresses if a.version == 4), addresses[0]))


MEDIA_TYPES = {
    "image": {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif", "image/heic", "image/heif"},
    "video": {"video/mp4", "video/webm", "video/quicktime", "video/x-m4v"},
}


class MediaResponse(StreamingResponse):
    """Close upstream even if the browser disconnects or streaming raises."""

    def __init__(self, upstream, client, **kwargs):
        super().__init__(upstream.aiter_raw(), **kwargs)
        self.upstream, self.client = upstream, client

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.upstream.aclose()
                finally:
                    await self.client.aclose()


async def proxy_media(url: str, kind: str, range_header: str | None = None):
    current = validate_media_url(url)
    if range_header and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
        raise HTTPException(400, "仅支持单段字节 Range 请求")
    # A dedicated client per stream: no XHS/login cookies, proxy env, or cross-host pool reuse.
    client = httpx.AsyncClient(
        trust_env=False, follow_redirects=False, verify=True, timeout=httpx.Timeout(20, connect=10),
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )
    response = None
    handed_off = False
    try:
        for hop in range(4):
            address = await resolve_public_ip(current.host, current.port or (443 if current.scheme == "https" else 80))
            headers = {
                "Host": current.host,
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": "Mozilla/5.0",
                "Accept-Encoding": "identity",
            }
            if range_header:
                headers["Range"] = range_header
            # Explicit Request avoids the client's cookie jar (including redirect Set-Cookie).
            request = httpx.Request(
                "GET", current.copy_with(host=address), headers=headers,
                extensions={"sni_hostname": current.host,
                            "timeout": {"connect": 10, "read": 20, "write": 10, "pool": 10}},
            )
            response = await client.send(request, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.aclose()
                if not location or hop == 3:
                    raise HTTPException(502, "媒体重定向无效或次数过多")
                current = validate_media_url(urljoin(str(current), location))
                continue
            if response.status_code not in {200, 206}:
                raise HTTPException(502, "上游媒体加载失败")
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if mime not in MEDIA_TYPES[kind] or response.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(502, "上游响应不是允许的媒体类型")
            safe_headers = {
                "Content-Type": mime,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            }
            for name in ("content-length", "content-range", "accept-ranges"):
                if name in response.headers:
                    safe_headers[name] = response.headers[name]
            result = MediaResponse(response, client, status_code=response.status_code, headers=safe_headers)
            handed_off = True
            return result
        raise HTTPException(502, "媒体重定向失败")
    except httpx.HTTPError:
        raise HTTPException(502, "媒体连接失败") from None
    finally:
        if not handed_off:
            with anyio.CancelScope(shield=True):
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await client.aclose()
