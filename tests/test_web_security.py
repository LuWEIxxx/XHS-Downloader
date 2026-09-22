import asyncio
import base64
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import web_security as security
import webui


PASSWORD = "test-only-password-123456"
ENV = {"XHS_WEB_USERNAME": "admin", "XHS_WEB_PASSWORD": PASSWORD}
AUTH = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
WRITE = AUTH | {"X-XHS-Request": "1"}
MEDIA_URL = "https://sns-img-bd.xhscdn.com/example"


def fake_state():
    state = MagicMock()
    state.ensure = AsyncMock(return_value=SimpleNamespace(manager=SimpleNamespace(
        path=Path("."), folder=Path("Download"), root=Path("."),
    )))
    state.settings.read.return_value = {"cookie": "secret-xhs-cookie", "folder_name": "Download"}
    state.update_settings = AsyncMock(return_value=state.settings.read.return_value)
    return state


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = fake_state()
        self.app = webui.create_app(self.state)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_fail_closed(self):
        for value in ("", "short"):
            with self.subTest(value=value), patch.dict(os.environ, {"XHS_WEB_PASSWORD": value}):
                with self.assertRaises(RuntimeError):
                    webui.create_app(self.state)
        self.state.ensure.assert_not_awaited()

    def test_all_routes_require_auth(self):
        for route in self.app.routes:
            for method in getattr(route, "methods", {"GET"}):
                with self.subTest(route=route.path, method=method):
                    response = self.client.request(method, route.path)
                    self.assertEqual(401, response.status_code)
                    self.assertIn("Basic", response.headers["www-authenticate"])
        self.state.ensure.assert_not_awaited()

    def test_malformed_and_wrong_credentials(self):
        for value in ("", "Basic !!!", "Bearer token", "Basic " + base64.b64encode(b"admin:wrong").decode()):
            with self.subTest(value=value):
                self.assertEqual(401, self.client.get("/api/settings", headers={"Authorization": value}).status_code)

    def test_page_vendor_docs_and_cookie_redaction(self):
        for path in ("/", "/vendor/vue.global.prod.js", "/docs", "/openapi.json", "/api/settings"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=AUTH)
                self.assertEqual(200, response.status_code)
                self.assertEqual("no-store", response.headers["cache-control"])
                self.assertNotIn("secret-xhs-cookie", response.text)
        data = self.client.get("/api/settings", headers=AUTH).json()["settings"]
        self.assertNotIn("cookie", data)
        self.assertTrue(data["cookie_configured"])

    def test_csrf_and_preflight(self):
        self.assertEqual(403, self.client.put("/api/settings", headers=AUTH, json={"settings": {}}).status_code)
        self.assertEqual(403, self.client.put("/api/settings", headers=WRITE | {"Sec-Fetch-Site": "cross-site"}, json={"settings": {}}).status_code)
        response = self.client.options("/api/settings", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-XHS-Request",
        })
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.state.update_settings.assert_not_awaited()

    def test_update_redacts_cookie_and_rejects_conflict(self):
        response = self.client.put("/api/settings", headers=WRITE, json={"settings": {}, "cookie": "new-secret"})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("secret-xhs-cookie", response.text)
        self.state.update_settings.assert_awaited_once_with({}, "new-secret", False)
        response = self.client.put("/api/settings", headers=WRITE, json={
            "settings": {}, "cookie": "new-secret", "clear_cookie": True,
        })
        self.assertEqual(400, response.status_code)

    def test_settings_errors_do_not_reflect_secrets(self):
        self.state.update_settings.side_effect = ValueError("secret-xhs-cookie")
        response = self.client.put("/api/settings", headers=WRITE, json={"settings": {}})
        self.assertEqual(500, response.status_code)
        self.assertNotIn("secret-xhs-cookie", response.text)

    def test_authenticated_media_rejects_invalid_url(self):
        self.assertEqual(400, self.client.get("/api/image", headers=AUTH, params={"url": "http://127.0.0.1/"}).status_code)
        self.state.ensure.assert_not_awaited()


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_preserve_replace_clear_and_script_server_disabled(self):
        for cookie, clear, expected in ((None, False, "old"), ("", False, "old"), ("  ", False, "old"), ("new", False, "new"), (None, True, "")):
            with self.subTest(cookie=cookie, clear=clear), patch.object(webui, "Settings") as settings, patch.object(webui, "XHS") as xhs:
                settings.return_value.read.return_value = {"cookie": "old", "script_server": True}
                instance = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
                xhs.return_value = instance
                state = webui.AppState()
                state.xhs = instance
                merged = await state.update_settings({"cookie": "legacy-value", "folder_name": "Download"}, cookie, clear)
                self.assertEqual(expected, merged["cookie"])
                self.assertFalse(xhs.call_args.kwargs["script_server"])

    async def test_initial_web_instance_disables_script_server(self):
        with patch.object(webui, "Settings") as settings, patch.object(webui, "XHS") as xhs:
            settings.return_value.run.return_value = {"script_server": True, "work_path": "."}
            xhs.return_value.__aenter__ = AsyncMock()
            state = webui.AppState()
            await state.ensure()
            self.assertFalse(xhs.call_args.kwargs["script_server"])


class URLTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_urls(self):
        for url in (
            "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/",
            "file:///etc/passwd", "https://evil.example/", "https://xhscdn.com.evil.example/",
            "https://evilxhscdn.com/", "https://sns.xhscdn.com@evil.example/",
            "https://ci.xiaohongshu.com.evil.example/", "https://www.xiaohongshu.com/",
            "https://user:pass@sns.xhscdn.com/", "https://sns.xhscdn.com:8443/",
            "https://sns.xhscdn.com/#fragment", "https://sns.xhscdn.com\\@127.0.0.1/",
            "https://sns.xhscdn.com/\n", "//sns.xhscdn.com/", "https://sns.xhscdn.com:bad/",
        ):
            with self.subTest(url=url), self.assertRaises(HTTPException):
                security.validate_media_url(url)

    def test_valid_cdn(self):
        for url in (MEDIA_URL, "http://sns-video-bd.xhscdn.com/a?x=1", "https://sns-webpic-qc.xhscdn.com/a"):
            self.assertTrue(security.validate_media_url(url).host.endswith(".xhscdn.com"))

    def test_default_generated_image_url_allowed(self):
        from source.application.image import Image
        url = Image._Image__generate_fixed_link("sample-token", "jpeg")
        self.assertEqual("ci.xiaohongshu.com", security.validate_media_url(url).host)

    async def test_private_and_mixed_dns_blocked(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1", "::1", "fe80::1", "fc00::1", "4000::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                       (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
            with self.subTest(ip=ip), patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=answers)):
                with self.assertRaises(HTTPException):
                    await security.resolve_public_ip("sns.xhscdn.com", 443)

    async def test_dns_failure_and_public_answer(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=socket.gaierror())):
            with self.assertRaises(HTTPException) as error:
                await security.resolve_public_ip("sns.xhscdn.com", 443)
            self.assertEqual(502, error.exception.status_code)
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
            self.assertEqual("8.8.8.8", await security.resolve_public_ip("sns.xhscdn.com", 443))


class Body(httpx.AsyncByteStream):
    def __init__(self, data=b"media"):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        self.closed = True


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def run_proxy(self, responses, kind="image", range_header=None):
        self.requests = []

        async def upstream(request):
            self.requests.append(request)
            return responses.pop(0)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.client = client
        with patch.object(security.httpx, "AsyncClient", return_value=client), patch.object(security, "resolve_public_ip", AsyncMock(return_value="8.8.8.8")) as dns:
            result = await security.proxy_media(MEDIA_URL, kind, range_header)
            self.dns_calls = dns.await_count
            return result

    async def consume(self, response):
        self.assertEqual(b"media", b"".join([part async for part in response.body_iterator]))
        await response.upstream.aclose()
        await response.client.aclose()

    async def test_pinned_ip_sni_cookie_isolation_and_headers(self):
        body = Body()
        response = await self.run_proxy([httpx.Response(200, headers={"Content-Type": "image/jpeg", "Set-Cookie": "secret=yes"}, stream=body)])
        request = self.requests[0]
        self.assertEqual("8.8.8.8", request.url.host)
        self.assertEqual("sns-img-bd.xhscdn.com", request.headers["host"])
        self.assertEqual("sns-img-bd.xhscdn.com", request.extensions["sni_hostname"])
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        await self.consume(response)
        self.assertTrue(body.closed)

    async def test_range_206(self):
        response = await self.run_proxy([httpx.Response(206, headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-4/10", "Content-Length": "5"}, stream=Body())], "video", "bytes=0-4")
        self.assertEqual(206, response.status_code)
        self.assertEqual("bytes 0-4/10", response.headers["content-range"])
        self.assertEqual("bytes=0-4", self.requests[0].headers["range"])
        await self.consume(response)

    async def test_reject_bad_range(self):
        for header in ("bytes=0-1,4-5", "items=0-5", "bytes=-", "bytes=0-5\r\nCookie: x"):
            with self.subTest(header=header), self.assertRaises(HTTPException):
                await security.proxy_media(MEDIA_URL, "video", header)

    async def test_block_html_svg_missing_mime_and_upstream_errors(self):
        for status, headers in ((200, {"Content-Type": "text/html"}), (200, {"Content-Type": "image/svg+xml"}), (200, {}), (200, {"Content-Type": "image/jpeg", "Content-Encoding": "gzip"}), (500, {"Content-Type": "image/jpeg"})):
            body = Body()
            with self.subTest(status=status, headers=headers), self.assertRaises(HTTPException):
                await self.run_proxy([httpx.Response(status, headers=headers, stream=body)])
            self.assertTrue(body.closed)
            self.assertTrue(self.client.is_closed)

    async def test_redirect_validated_and_cookies_not_forwarded(self):
        first = Body()
        response = await self.run_proxy([
            httpx.Response(302, headers={"Location": "/next", "Set-Cookie": "upstream=secret; Path=/"}, stream=first),
            httpx.Response(200, headers={"Content-Type": "image/png"}, stream=Body()),
        ])
        self.assertEqual(2, self.dns_calls)
        self.assertTrue(first.closed)
        self.assertEqual("/next", self.requests[1].url.path)
        self.assertNotIn("cookie", self.requests[1].headers)
        await self.consume(response)

    async def test_redirect_to_private_or_external_host_blocked(self):
        for location in ("http://127.0.0.1/", "https://evil.example/"):
            with self.subTest(location=location), self.assertRaises(HTTPException):
                await self.run_proxy([httpx.Response(302, headers={"Location": location}, stream=Body())])
            self.assertEqual(1, len(self.requests))
            self.assertTrue(self.client.is_closed)

    async def test_redirect_loop_bounded(self):
        with self.assertRaises(HTTPException):
            await self.run_proxy([httpx.Response(302, headers={"Location": "/loop"}, stream=Body()) for _ in range(4)])
        self.assertEqual(4, len(self.requests))
        self.assertTrue(self.client.is_closed)

    async def test_redirect_dns_rebinding_blocked_before_second_request(self):
        first = Body()
        send = AsyncMock(return_value=httpx.Response(302, headers={"Location": "/next"}, stream=first))
        client = SimpleNamespace(send=send, aclose=AsyncMock())
        dns = AsyncMock(side_effect=["8.8.8.8", HTTPException(400, "blocked")])
        with patch.object(security.httpx, "AsyncClient", return_value=client), patch.object(security, "resolve_public_ip", dns):
            with self.assertRaises(HTTPException):
                await security.proxy_media(MEDIA_URL, "image")
        self.assertEqual(2, dns.await_count)
        send.assert_awaited_once()
        client.aclose.assert_awaited_once()
        self.assertTrue(first.closed)

    async def test_transport_failures_close_client_without_reflecting_details(self):
        for failure in (httpx.ConnectError("internal-address"), httpx.ReadTimeout("internal-address")):
            client = SimpleNamespace(send=AsyncMock(side_effect=failure), aclose=AsyncMock())
            with self.subTest(failure=type(failure)), patch.object(security.httpx, "AsyncClient", return_value=client), patch.object(security, "resolve_public_ip", AsyncMock(return_value="8.8.8.8")):
                with self.assertRaises(HTTPException) as error:
                    await security.proxy_media(MEDIA_URL, "image")
                self.assertEqual(502, error.exception.status_code)
                self.assertNotIn("internal-address", error.exception.detail)
                client.aclose.assert_awaited_once()

    async def test_media_route_streams_through_auth_middleware(self):
        upstream_body = Body()
        media_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Type": "image/png"}, stream=upstream_body)
        ))
        with patch.dict(os.environ, ENV):
            app = webui.create_app(fake_state())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as browser_client:
            with patch.object(security.httpx, "AsyncClient", return_value=media_client), patch.object(security, "resolve_public_ip", AsyncMock(return_value="8.8.8.8")):
                response = await browser_client.get("/api/image", params={"url": MEDIA_URL}, headers=AUTH)
        self.assertEqual(200, response.status_code)
        self.assertEqual(b"media", response.content)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertTrue(upstream_body.closed)
        self.assertTrue(media_client.is_closed)

    async def test_stream_send_failure_closes_resources(self):
        body = Body()
        response = await self.run_proxy([httpx.Response(200, headers={"Content-Type": "image/png"}, stream=body)])
        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), AsyncMock(side_effect=OSError("disconnected")))
        self.assertTrue(body.closed)
        self.assertTrue(self.client.is_closed)

    async def test_cancelled_stream_closes_resources(self):
        body = Body()
        response = await self.run_proxy([httpx.Response(200, headers={"Content-Type": "image/png"}, stream=body)])
        with self.assertRaises(asyncio.CancelledError):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), AsyncMock(side_effect=asyncio.CancelledError()))
        self.assertTrue(body.closed)
        self.assertTrue(self.client.is_closed)


if __name__ == "__main__":
    unittest.main()
