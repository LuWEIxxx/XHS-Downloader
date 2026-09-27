"""WebUI 与新版 XHS.extract() 接口契约测试。

覆盖点：
1. /api/parse 不再传 data=True，且传 check_record=False（预览不受下载记录影响）
2. /api/download 不再访问 skip_download()
3. 新作品返回 ok=True 且统计文件数正确
4. 命中下载记录返回 ok=True, skipped=True
5. 无作品数据返回明确 400
"""
import base64
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import webui


PASSWORD = "test-only-password-123456"
ENV = {"XHS_WEB_USERNAME": "admin", "XHS_WEB_PASSWORD": PASSWORD}
AUTH = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
WRITE = AUTH | {"X-XHS-Request": "1"}

WORK = {
    "作品ID": "abc123",
    "作品类型": "图文",
    "作品标题": "测试作品",
    "作者昵称": "测试作者",
    "作者ID": "user1",
    "作品链接": "https://www.xiaohongshu.com/explore/abc123",
    "下载地址": ["https://sns-img-bd.xhscdn.com/a", "https://sns-img-bd.xhscdn.com/b"],
    "动图地址": [None, None],
}

VIDEO_WORK = {
    "作品ID": "v1",
    "作品类型": "视频",
    "作品标题": "视频作品",
    "作者昵称": "作者",
    "作者ID": "u1",
    "作品链接": "https://www.xiaohongshu.com/explore/v1",
    "下载地址": ["https://sns-video-bd.xhscdn.com/v.mp4"],
    "动图地址": [None],
}


class FakeXHS:
    """符合新版 extract() 签名的假 XHS，记录调用参数。"""

    def __init__(self, results):
        self.results = results
        self.calls = []
        self.manager = SimpleNamespace(
            path=Path("."),
            folder=Path("Download"),
            root=Path("."),
            download_record=True,
        )

    async def extract(
        self,
        url,
        download=False,
        index=None,
        check_record=True,
        progress_callback=None,
        task_id=None,
        result_callback=None,
        proxy=None,
    ):
        self.calls.append(
            {
                "url": url,
                "download": download,
                "index": index,
                "check_record": check_record,
                "proxy": proxy,
            }
        )
        return self.results

    # 故意不定义 skip_download：若 WebUI 仍调用会立刻 AttributeError


def fake_state(xhs):
    state = MagicMock()
    state.ensure = AsyncMock(return_value=xhs)
    state.settings.read.return_value = {"cookie": "", "folder_name": "Download"}
    return state


class ParseContractTests(unittest.TestCase):
    """解析接口契约：不带 data=True，且忽略下载记录。"""

    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _client(self, results):
        xhs = FakeXHS(results)
        client = TestClient(webui.create_app(fake_state(xhs)))
        self.addCleanup(client.close)
        return client, xhs

    def test_parse_passes_check_record_false_and_no_data_kwarg(self):
        client, xhs = self._client([WORK])
        response = client.post("/api/parse", json={"url": WORK["作品链接"]}, headers=WRITE)
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])
        call = xhs.calls[0]
        self.assertFalse(call["check_record"], "解析预览必须忽略下载记录")
        self.assertFalse(call["download"])
        # 旧签名参数 data 不应再出现
        self.assertNotIn("data", call)

    def test_parse_returns_works_with_items(self):
        client, _ = self._client([WORK])
        body = client.post("/api/parse", json={"url": WORK["作品链接"]}, headers=WRITE).json()
        self.assertEqual(1, body["total"])
        work = body["works"][0]
        self.assertEqual("abc123", work["作品ID"])
        self.assertEqual(2, len(work["items"]))
        self.assertTrue(work["items"][0]["preview"].startswith("/api/image?url="))

    def test_parse_empty_result_returns_400(self):
        client, _ = self._client([{"message": "作品 abc 存在下载记录，跳过处理"}])
        response = client.post("/api/parse", json={"url": "u"}, headers=WRITE)
        self.assertEqual(400, response.status_code)
        self.assertFalse(response.json()["ok"])


class DownloadContractTests(unittest.TestCase):
    """下载接口契约：不调用 skip_download，正确区分新作品/已跳过。"""

    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _client(self, results):
        xhs = FakeXHS(results)
        client = TestClient(webui.create_app(fake_state(xhs)))
        self.addCleanup(client.close)
        return client, xhs

    def test_download_does_not_access_skip_download(self):
        client, xhs = self._client([WORK])
        response = client.post("/api/download", json={"url": WORK["作品链接"]}, headers=WRITE)
        self.assertEqual(200, response.status_code)
        self.assertFalse(hasattr(xhs, "skip_download"))
        self.assertNotIn("skipped", xhs.calls[0])
        self.assertTrue(xhs.calls[0]["check_record"])

    def test_new_work_returns_ok_with_correct_count(self):
        client, _ = self._client([WORK])
        body = client.post("/api/download", json={"url": "u"}, headers=WRITE).json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["skipped"])
        # 图文作品 2 个下载地址 → 2 个文件
        self.assertIn("2", body["message"])
        self.assertIn("data", body)

    def test_video_work_count_is_one(self):
        client, _ = self._client([VIDEO_WORK])
        body = client.post("/api/download", json={"url": "u"}, headers=WRITE).json()
        self.assertTrue(body["ok"])
        self.assertIn("1", body["message"])

    def test_index_overrides_count(self):
        client, _ = self._client([WORK])
        body = client.post(
            "/api/download", json={"url": "u", "index": [1]}, headers=WRITE
        ).json()
        self.assertTrue(body["ok"])
        self.assertIn("1", body["message"])

    def test_existing_record_returns_skipped(self):
        client, _ = self._client([{"message": "作品 abc 存在下载记录，跳过处理"}])
        body = client.post("/api/download", json={"url": "u"}, headers=WRITE).json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["skipped"])
        self.assertIn("下载记录", body["message"])
        self.assertIn("download_path", body)

    def test_no_data_returns_400(self):
        client, _ = self._client([])
        response = client.post("/api/download", json={"url": "u"}, headers=WRITE)
        self.assertEqual(400, response.status_code)
        self.assertFalse(response.json()["ok"])

    def test_unknown_payload_returns_400(self):
        client, _ = self._client([{"foo": "bar"}])
        response = client.post("/api/download", json={"url": "u"}, headers=WRITE)
        self.assertEqual(400, response.status_code)


class CountHelperTests(unittest.TestCase):
    """_count_download_files 不得使用前端 items 字段。"""

    def test_image_work_counts_download_urls(self):
        self.assertEqual(2, webui._count_download_files(WORK))

    def test_video_work_counts_one(self):
        self.assertEqual(1, webui._count_download_files(VIDEO_WORK))

    def test_video_without_url_counts_zero(self):
        self.assertEqual(0, webui._count_download_files({**VIDEO_WORK, "下载地址": []}))

    def test_index_takes_precedence(self):
        self.assertEqual(3, webui._count_download_files(WORK, [1, 2, 3]))

    def test_empty_payload(self):
        self.assertEqual(0, webui._count_download_files({}))


if __name__ == "__main__":
    unittest.main()
