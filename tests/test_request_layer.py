"""请求层测试：验证 Html 使用 curl_cffi 并按预期传参。

覆盖点：
1. 无 Cookie 走 AsyncSession（manager.request_client）
2. 有 Cookie 走 curl_cffi.requests.get 且使用 cookies= 参数（非裸 Cookie 头）
3. 传入 impersonate
4. Referer 由 manager.get_headers(url) 动态生成
5. 临时 proxy 可覆盖 manager 默认 proxy
6. RequestException 被捕获并返回空字符串
7. 不使用 HTTPX 作为核心请求客户端
"""
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from curl_cffi.requests.exceptions import RequestException

import source.application.request as request_module
from source.application.request import Html


def fake_manager(
    *,
    proxy="http://default-proxy:1080",
    impersonate="chrome146",
    cookie="a=1; b=2",
    text="<html>ok</html>",
    raise_error=None,
):
    """构造可注入的假 Manager。"""
    response = MagicMock()
    response.text = text
    response.url = "https://www.xiaohongshu.com/explore/abc"
    response.status_code = 200
    if raise_error:
        response.raise_for_status.side_effect = raise_error
    else:
        response.raise_for_status.return_value = None

    session = MagicMock()
    session.get = AsyncMock(return_value=response)

    manager = MagicMock()
    manager.print = MagicMock()
    manager.retry = 1
    manager.timeout = 10
    manager.proxy = proxy
    manager.impersonate = impersonate
    manager.request_client = session
    manager.get_headers = MagicMock(return_value={"Referer": "https://www.xiaohongshu.com/"})
    manager.cookie_str_to_dict = MagicMock(return_value={"a": "1", "b": "2"})
    return manager


def run(coro):
    import asyncio

    return asyncio.run(coro)


class NoCookieTests(unittest.TestCase):
    """无 Cookie：应使用 manager.request_client 的异步会话。"""

    def test_uses_async_session(self):
        manager = fake_manager()
        html = Html(manager)
        with patch.object(request_module, "sleep_time", AsyncMock()):
            result = run(html.request_url("https://www.xiaohongshu.com/explore/abc"))

        self.assertEqual("<html>ok</html>", result)
        manager.request_client.get.assert_awaited_once()
        args, kwargs = manager.request_client.get.call_args
        self.assertEqual("https://www.xiaohongshu.com/explore/abc", args[0])
        self.assertIn("headers", kwargs)
        # 未传 proxy 时使用 manager 默认 proxy
        self.assertEqual("http://default-proxy:1080", kwargs["proxy"])

    def test_dynamic_referer_used(self):
        manager = fake_manager()
        html = Html(manager)
        with patch.object(request_module, "sleep_time", AsyncMock()):
            run(html.request_url("https://www.xiaohongshu.com/explore/abc"))
        manager.get_headers.assert_called_once_with(
            "https://www.xiaohongshu.com/explore/abc"
        )

    def test_url_without_scheme_gets_https(self):
        manager = fake_manager()
        html = Html(manager)
        with patch.object(request_module, "sleep_time", AsyncMock()):
            run(html.request_url("www.xiaohongshu.com/explore/abc"))
        args, _ = manager.request_client.get.call_args
        self.assertTrue(args[0].startswith("https://"))


class WithCookieTests(unittest.TestCase):
    """有 Cookie：应走 curl_cffi.requests.get 并使用 cookies= 参数。"""

    def test_uses_get_with_cookies_param(self):
        manager = fake_manager()
        html = Html(manager)
        sentinel = MagicMock()
        sentinel.text = "<html>cookie</html>"
        sentinel.raise_for_status.return_value = None

        with patch.object(request_module, "get", return_value=sentinel) as mock_get, \
                patch.object(request_module, "sleep_time", AsyncMock()):
            result = run(
                html.request_url(
                    "https://www.xiaohongshu.com/explore/abc",
                    cookie="a=1; b=2",
                )
            )

        self.assertEqual("<html>cookie</html>", result)
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        # 必须用 cookies= 而非裸 Cookie 头
        self.assertEqual({"a": "1", "b": "2"}, kwargs["cookies"])
        self.assertNotIn("Cookie", kwargs.get("headers", {}))
        # 关键 curl_cffi 参数
        self.assertEqual("chrome146", kwargs["impersonate"])
        self.assertFalse(kwargs["verify"])
        self.assertTrue(kwargs["allow_redirects"])
        self.assertEqual(10, kwargs["timeout"])
        manager.cookie_str_to_dict.assert_called_once_with("a=1; b=2")

    def test_manager_cookie_conversion_is_used(self):
        manager = fake_manager()
        html = Html(manager)
        sentinel = MagicMock()
        sentinel.text = "x"
        sentinel.raise_for_status.return_value = None
        with patch.object(request_module, "get", return_value=sentinel), \
                patch.object(request_module, "sleep_time", AsyncMock()):
            run(html.request_url("https://a.com", cookie="raw-cookie"))
        manager.cookie_str_to_dict.assert_called_once_with("raw-cookie")


class ProxyTests(unittest.TestCase):
    """临时 proxy 优先于 manager 默认 proxy。"""

    def test_temp_proxy_overrides_default_no_cookie(self):
        manager = fake_manager(proxy="http://default:1080")
        html = Html(manager)
        with patch.object(request_module, "sleep_time", AsyncMock()):
            run(html.request_url("https://a.com", proxy="http://temp:8080"))
        _, kwargs = manager.request_client.get.call_args
        self.assertEqual("http://temp:8080", kwargs["proxy"])

    def test_temp_proxy_overrides_default_with_cookie(self):
        manager = fake_manager(proxy="http://default:1080")
        html = Html(manager)
        sentinel = MagicMock()
        sentinel.text = "x"
        sentinel.raise_for_status.return_value = None
        with patch.object(request_module, "get", return_value=sentinel) as mock_get, \
                patch.object(request_module, "sleep_time", AsyncMock()):
            run(html.request_url("https://a.com", cookie="a=1", proxy="http://temp:8080"))
        _, kwargs = mock_get.call_args
        self.assertEqual("http://temp:8080", kwargs["proxy"])


class ErrorHandlingTests(unittest.TestCase):
    """RequestException 应被捕获并返回空字符串。"""

    def test_request_exception_returns_empty(self):
        manager = fake_manager(raise_error=RequestException("boom"))
        html = Html(manager)
        with patch.object(request_module, "sleep_time", AsyncMock()):
            result = run(html.request_url("https://a.com"))
        self.assertEqual("", result)

    def test_logging_does_not_leak_cookie(self):
        manager = fake_manager(raise_error=RequestException("boom"))
        html = Html(manager)
        with patch.object(request_module, "sleep_time", AsyncMock()), \
                patch.object(request_module, "logging") as mock_log:
            run(html.request_url("https://a.com", cookie="secret-cookie-value"))
        for call in mock_log.call_args_list:
            rendered = " ".join(str(a) for a in call.args)
            self.assertNotIn("secret-cookie-value", rendered)


class NoHttpxTests(unittest.TestCase):
    """核心请求层不得再依赖 HTTPX。"""

    def test_request_module_has_no_httpx(self):
        source = inspect.getsource(request_module)
        self.assertNotIn("httpx", source.lower())
        self.assertIn("curl_cffi", source)

    def test_exception_type_is_curl_cffi(self):
        source = inspect.getsource(request_module)
        self.assertIn("RequestException", source)
        self.assertNotIn("HTTPError", source)


if __name__ == "__main__":
    unittest.main()
