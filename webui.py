"""XHS-Downloader Web UI 扩展

在开源项目 FastAPI API 模式基础上，新增浏览器前端页面所需的接口：
- POST /api/parse      解析作品链接，返回预览数据（不下载）
- POST /api/download   按选中序号下载作品文件
- GET  /api/settings   读取当前设置
- PUT  /api/settings   更新设置（写入 settings.json 并热重建 XHS 实例）
- GET  /api/dirs       浏览容器内目录（选择保存位置）
- GET  /api/image      图片代理（解决小红书防盗链，前端可预览）
- GET  /api/video      视频代理（支持 Range 请求，可拖动进度条）
- GET  /               托管前端页面（static/web/index.html）

用法：python main.py web
"""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import get_args
from urllib.parse import quote

import aiosqlite
from curl_cffi.requests import BrowserTypeLiteral
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from source import Settings, XHS
from source.module import IMPERSONATE
from source.module.recorder import RECORD_COLUMNS_EXPECTED
from web_security import WebAuth, proxy_media, public_settings

__all__ = ["AppState", "create_app"]

# 前端页面目录（webui.py 位于项目根目录）
WEB_DIR = Path(__file__).resolve().parent.joinpath("static", "web")

# 浏览器模拟目标候选：直接跟随 curl_cffi 支持列表，避免上游升级后失配
IMPERSONATE_OPTIONS = ["", *get_args(BrowserTypeLiteral)]
IMPERSONATE_HINT = f"curl_cffi 浏览器指纹模拟目标，留空使用项目默认值（{IMPERSONATE}）；用于绕过小红书风控"

# 设置项描述（前端据此渲染表单）
SETTINGS_SCHEMA = [
    {"key": "work_path", "type": "dir", "label": "保存根路径", "default": "",
     "hint": "作品数据/文件保存根路径；留空则使用默认目录（容器内 /app/Volume）"},
    {"key": "folder_name", "type": "text", "label": "下载文件夹名称", "default": "Download",
     "hint": "作品文件储存文件夹名称（自动创建）"},
    {"key": "name_format", "type": "text", "label": "文件命名格式", "default": "发布时间 作者昵称 作品标题",
     "hint": "字段间用空格分隔，支持：收藏数量/评论数量/分享数量/点赞数量/作品标签/作品ID/作品标题/作品描述/作品类型/发布时间/最后更新时间/作者昵称/作者ID"},
    {"key": "cookie", "type": "password", "label": "Cookie", "default": "",
     "hint": "小红书网页版 Cookie，无需登录，非必需；设置后可获取视频高清画质"},
    {"key": "proxy", "type": "text", "label": "代理", "default": "",
     "hint": "请求数据时使用的代理，如 http://127.0.0.1:10808"},
    {"key": "proxy_download", "type": "bool", "label": "下载文件使用代理", "default": False,
     "hint": "下载作品文件时使用上方配置的网络代理；关闭时仅请求数据走代理"},
    {"key": "impersonate", "type": "select", "label": "浏览器模拟目标", "default": "",
     "options": IMPERSONATE_OPTIONS,
     "hint": IMPERSONATE_HINT},
    {"key": "timeout", "type": "number", "label": "请求超时（秒）", "default": 10, "hint": "请求数据超时限制"},
    {"key": "max_retry", "type": "number", "label": "最大重试次数", "default": 5, "hint": "请求数据失败时的重试次数"},
    {"key": "chunk", "type": "number", "label": "下载块大小（字节）", "default": 2097152,
     "hint": "下载文件时每次从服务器获取的数据块大小"},
    {"key": "image_format", "type": "select", "label": "图文下载格式", "default": "JPEG",
     "options": ["AUTO", "PNG", "WEBP", "JPEG", "HEIC"],
     "hint": "AUTO 表示动态格式，实际格式取决于服务器响应数据"},
    {"key": "video_preference", "type": "select", "label": "视频下载偏好", "default": "resolution",
     "options": [("resolution", "分辨率优先"), ("bitrate", "码率优先"), ("size", "文件大小优先")],
     "hint": "视频作品文件下载偏好"},
    {"key": "language", "type": "select", "label": "程序语言", "default": "zh_CN",
     "options": [("zh_CN", "简体中文"), ("en_US", "English")], "hint": "程序提示语言"},
    {"key": "note_format", "type": "select", "label": "作品信息保存格式", "default": "",
     "options": [("", "不保存"), ("txt", "纯文本"), ("md", "Markdown"), ("all", "全部格式")],
     "hint": "每个作品单独保存信息文件"},
    {"key": "record_data", "type": "bool", "label": "保存作品数据至数据库", "default": False,
     "hint": "所有作品数据统一保存至 SQLite 数据库"},
    {"key": "image_download", "type": "bool", "label": "图文作品下载开关", "default": True, "hint": "是否下载图文、图集作品文件"},
    {"key": "video_download", "type": "bool", "label": "视频作品下载开关", "default": True, "hint": "是否下载视频作品文件"},
    {"key": "video_cover_download", "type": "bool", "label": "视频封面下载开关", "default": False, "hint": "下载视频作品时同时保存封面图"},
    {"key": "live_download", "type": "bool", "label": "实况照片下载开关", "default": False, "hint": "下载动图并直接生成同名 JPG+MOV 实况照片文件"},
    {"key": "folder_mode", "type": "bool", "label": "单作品独立文件夹", "default": False,
     "hint": "每个作品的文件储存至单独文件夹"},
    {"key": "download_record", "type": "bool", "label": "记录下载历史", "default": True,
     "hint": "记录下载成功的作品 ID，重复下载时自动跳过"},
    {"key": "author_archive", "type": "bool", "label": "按作者归档", "default": False,
     "hint": "每个作者的作品储存至单独文件夹"},
    {"key": "write_mtime", "type": "bool", "label": "写入修改时间", "default": False,
     "hint": "将作品文件的修改时间改为作品发布时间"},
]

# 首次启动时探测的下载挂载目录（docker-compose 通常把 NAS 共享目录挂载到这里）
DEFAULT_MOUNT_CANDIDATES = ("/downloads", "/data", "/download")


def resolve_default_work_path(settings: dict) -> str:
    """work_path 未设置时，探测容器内挂载的下载目录，返回命中项（避免文件落入 docker 卷）"""
    if settings.get("work_path"):
        return settings["work_path"]
    for candidate in DEFAULT_MOUNT_CANDIDATES:
        if Path(candidate).is_dir():
            return candidate
    return ""


# ExploreID.db 下载记录扩展字段（与 IDRecorder.RECORD_COLUMNS 保持同一来源）
EXPLORE_ID_COLUMNS = RECORD_COLUMNS_EXPECTED


async def _migrate_explore_id(db) -> None:
    """确保 explore_id 表包含全部扩展列（旧库缺失时 ALTER TABLE 补充）"""
    cursor = await db.execute("PRAGMA table_info(explore_id)")
    existing = {row[1] for row in await cursor.fetchall()}
    for column in EXPLORE_ID_COLUMNS:
        if column not in existing:
            await db.execute(f"ALTER TABLE explore_id ADD COLUMN {column} TEXT")
    await db.commit()


class AppState:
    """管理 XHS 实例的创建与热重建（设置变更后重新实例化）"""

    def __init__(self):
        self.init_lock = asyncio.Lock()  # 仅保护首次初始化
        self.op_lock = asyncio.Lock()  # 串行化解析/下载/设置更新
        self.settings = Settings()
        self.xhs: XHS | None = None

    async def ensure(self) -> XHS:
        """确保 XHS 实例已初始化并返回"""
        if self.xhs is None:
            async with self.init_lock:
                if self.xhs is None:
                    settings = self.settings.run()
                    if path := resolve_default_work_path(settings):
                        if path != settings.get("work_path"):
                            # 探测到下载挂载目录且用户未显式设置：写入配置使其透明可见
                            settings["work_path"] = path
                            self.settings.update(settings)
                            print(f"[WebUI] 已自动将保存根路径设为 {path}（检测到下载挂载目录）")
                    # The standalone script server does not share Web authentication.
                    self.xhs = XHS(**(settings | {"script_server": False}))
                    await self.xhs.__aenter__()
        return self.xhs

    async def update_settings(self, data: dict, cookie: str | None = None, clear_cookie: bool = False) -> dict:
        """合并更新设置：写盘 -> 关闭旧实例 -> 用新配置重建"""
        async with self.op_lock:
            await self.ensure()
            current = self.settings.read()
            merged = {**current, **{k: v for k, v in data.items() if k in SETTINGS_SCHEMA_BY_KEY and k != "cookie"}}
            if clear_cookie:
                merged["cookie"] = ""
            elif cookie and cookie.strip():
                merged["cookie"] = cookie.strip()
            self.settings.update(merged)
            await self.xhs.__aexit__(None, None, None)
            self.xhs = XHS(**(merged | {"script_server": False}))
            await self.xhs.__aenter__()
            return merged


SETTINGS_SCHEMA_BY_KEY = {item["key"]: item for item in SETTINGS_SCHEMA}


def build_work_payload(data: dict) -> dict:
    """把作品数据转换为前端可用的结构（items 包含代理预览地址）"""
    type_ = data.get("作品类型", "")
    urls = data.get("下载地址") or []
    lives = data.get("动图地址") or []
    items = []
    if type_ == "视频":
        if urls:
            items.append({
                "index": 1,
                "type": "video",
                "url": urls[0],
                "preview": f"/api/video?url={quote(urls[0], safe='')}",
            })
    else:
        for i, u in enumerate(urls, start=1):
            item = {
                "index": i,
                "type": "image",
                "url": u,
                "preview": f"/api/image?url={quote(u, safe='')}",
            }
            live = lives[i - 1] if i - 1 < len(lives) else None
            if live:
                item["live"] = f"/api/image?url={quote(live, safe='')}"
            items.append(item)
    payload = {k: v for k, v in data.items()}
    payload["items"] = items
    return payload


def _count_download_files(data: dict, index: list[int] | None = None) -> int:
    """统计本次下载涉及的文件数量。

    注意：不能用 build_work_payload() 产出的 items（那是前端字段），
    必须基于 extract() 原始返回的「下载地址」计算。
    """
    if index:
        return len(index)
    if data.get("作品类型") == "视频":
        return 1 if data.get("下载地址") else 0
    return len(data.get("下载地址") or [])


class ParseRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    index: list[int] | None = None


class SettingsUpdate(BaseModel):
    settings: dict
    cookie: str | None = None
    clear_cookie: bool = False


def create_app(state: AppState) -> FastAPI:
    auth = WebAuth()  # Fail closed before startup or any routes are served.
    app = FastAPI(
        title="XHS-Downloader Web",
        version="1.0.0",
        description="小红书作品下载 Web 管理界面",
    )
    app.middleware("http")(auth)

    # 兼容原有 API 模式接口
    @app.on_event("startup")
    async def startup():
        await state.ensure()

    # ---------- 作品解析 ----------
    @app.post("/api/parse")
    async def parse_work(req: ParseRequest):
        xhs = await state.ensure()
        async with state.op_lock:
            try:
                # 解析预览必须忽略下载记录，否则已下载作品无法再次预览
                results = await xhs.extract(
                    req.url,
                    download=False,
                    check_record=False,
                )
            except Exception as error:
                return JSONResponse({"ok": False, "message": f"解析异常：{error}"}, status_code=500)
        works = []
        for data in results:
            if data and data.get("作品ID"):
                works.append(build_work_payload(data))
        if not works:
            return JSONResponse(
                {"ok": False, "message": "未解析到有效作品数据（可能被风控，建议配置 Cookie 后重试）"},
                status_code=400,
            )
        return {"ok": True, "works": works, "total": len(works)}

    # ---------- 下载 ----------
    @app.post("/api/download")
    async def download_work(req: DownloadRequest):
        xhs = await state.ensure()
        async with state.op_lock:
            try:
                results = await xhs.extract(
                    req.url,
                    download=True,
                    index=req.index or None,
                )
            except Exception as error:
                return JSONResponse({"ok": False, "message": f"下载异常：{error}"}, status_code=500)
            data = results[0] if results else None
        if not data:
            return JSONResponse({"ok": False, "message": "获取作品数据失败，未执行下载"}, status_code=400)
        # 命中下载记录：extract 返回 {"message": ...}，无作品数据
        if data.get("message") and not data.get("作品ID"):
            return {
                "ok": True,
                "skipped": True,
                "message": data["message"],
                "download_path": str(xhs.manager.folder),
            }
        if not data.get("作品ID"):
            return JSONResponse({"ok": False, "message": "获取作品数据失败，未执行下载"}, status_code=400)
        count = _count_download_files(data, req.index)
        return {
            "ok": True,
            "message": f"下载任务执行完毕，共 {count} 个文件",
            "skipped": False,
            "download_path": str(xhs.manager.folder),
            "data": build_work_payload(data),
        }

    # ---------- 设置 ----------
    @app.get("/api/settings")
    async def get_settings():
        xhs = await state.ensure()
        return {
            "ok": True,
            "settings": public_settings(state.settings.read()),
            "schema": SETTINGS_SCHEMA,
            "effective": {
                "work_path": str(xhs.manager.path),
                "download_path": str(xhs.manager.folder),
                "folder_name": xhs.manager.folder.name,
            },
        }

    @app.put("/api/settings")
    async def put_settings(req: SettingsUpdate):
        if req.clear_cookie and req.cookie and req.cookie.strip():
            return JSONResponse({"ok": False, "message": "不能同时设置和清除 Cookie"}, status_code=400)
        try:
            merged = await state.update_settings(req.settings, req.cookie, req.clear_cookie)
        except Exception:
            return JSONResponse({"ok": False, "message": "设置保存失败，请检查配置"}, status_code=500)
        return {"ok": True, "message": "设置已保存并生效", "settings": public_settings(merged)}

    # ---------- 目录浏览 ----------
    @app.get("/api/dirs")
    async def list_dirs(path: str = "/"):
        try:
            p = Path(path).resolve()
            if not p.is_dir():
                return JSONResponse({"ok": False, "message": "目录不存在"}, status_code=400)
            entries = []
            for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if child.name.startswith((".", "$")):
                    continue
                try:
                    entries.append({
                        "name": child.name,
                        "path": str(child.resolve()),
                        "is_dir": child.is_dir(),
                    })
                except OSError:
                    continue
            parent = str(p.parent) if p != p.parent else None
            return {"ok": True, "path": str(p), "parent": parent, "entries": entries}
        except PermissionError:
            return JSONResponse({"ok": False, "message": "无权限访问该目录"}, status_code=403)

    # ---------- 下载目录文件浏览 ----------
    @app.get("/api/files")
    async def list_files(path: str = ""):
        """列出下载目录（或指定目录）下的文件，帮助确认文件保存位置"""
        xhs = await state.ensure()
        base = Path(path).resolve() if path else xhs.manager.folder
        try:
            if not base.is_dir():
                return JSONResponse({"ok": False, "message": "目录不存在"}, status_code=400)
            entries = []
            for child in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if child.name.startswith((".", "$")):
                    continue
                try:
                    stat = child.stat()
                    entries.append({
                        "name": child.name,
                        "path": str(child.resolve()),
                        "is_dir": child.is_dir(),
                        "size": stat.st_size if child.is_file() else 0,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except OSError:
                    continue
            parent = str(base.parent) if base != base.parent else None
            return {"ok": True, "path": str(base), "parent": parent, "files": entries}
        except PermissionError:
            return JSONResponse({"ok": False, "message": "无权限访问该目录"}, status_code=403)

    # ---------- 下载记录 ----------
    @app.get("/api/records")
    async def get_records():
        """查看下载记录（ExploreID.db），含作者昵称/作者ID/作品链接/作品标题/下载时间"""
        xhs = await state.ensure()
        ids_file = xhs.manager.root.joinpath("ExploreID.db")
        records = []
        if ids_file.is_file():
            try:
                async with aiosqlite.connect(ids_file) as db:
                    await _migrate_explore_id(db)
                    async with db.execute(
                        "SELECT ID, 作者昵称, 作者ID, 作品链接, 作品标题, 下载时间 "
                        "FROM explore_id ORDER BY rowid DESC"
                    ) as cur:
                        records = [
                            {
                                "id": r[0],
                                "作者昵称": r[1],
                                "作者ID": r[2],
                                "作品链接": r[3],
                                "作品标题": r[4],
                                "下载时间": r[5],
                            }
                            for r in await cur.fetchall()
                        ]
            except Exception as error:
                return JSONResponse(
                    {"ok": False, "message": f"读取下载记录失败：{error}"}, status_code=500
                )
        return {
            "ok": True,
            "records": records,
            "total": len(records),
            "record_enabled": bool(xhs.manager.download_record),
            "records_file": str(ids_file),
        }

    @app.delete("/api/records")
    async def delete_records(ids: str = ""):
        """删除指定下载记录，ids 为逗号分隔的作品 ID"""
        xhs = await state.ensure()
        ids_file = xhs.manager.root.joinpath("ExploreID.db")
        if not ids_file.is_file():
            return JSONResponse({"ok": False, "message": "暂无下载记录"}, status_code=400)
        ids_list = [i for i in ids.split(",") if i]
        if not ids_list:
            return JSONResponse({"ok": False, "message": "未指定要删除的记录"}, status_code=400)
        try:
            async with aiosqlite.connect(ids_file) as db:
                for i in ids_list:
                    await db.execute("DELETE FROM explore_id WHERE ID=?", (i,))
                await db.commit()
        except Exception as error:
            return JSONResponse({"ok": False, "message": f"删除失败：{error}"}, status_code=500)
        return {"ok": True, "message": f"已删除 {len(ids_list)} 条记录"}

    @app.post("/api/records/clear")
    async def clear_records():
        """清空全部下载记录"""
        xhs = await state.ensure()
        ids_file = xhs.manager.root.joinpath("ExploreID.db")
        try:
            if ids_file.is_file():
                async with aiosqlite.connect(ids_file) as db:
                    await db.execute("DELETE FROM explore_id")
                    await db.commit()
        except Exception as error:
            return JSONResponse({"ok": False, "message": f"清空失败：{error}"}, status_code=500)
        return {"ok": True, "message": "已清空全部下载记录"}

    @app.get("/api/records/export")
    async def export_records():
        """导出下载记录为 CSV（UTF-8 BOM，Excel 可直接打开）"""
        import csv
        from io import StringIO

        xhs = await state.ensure()
        ids_file = xhs.manager.root.joinpath("ExploreID.db")
        rows: list[tuple] = []
        if ids_file.is_file():
            try:
                async with aiosqlite.connect(ids_file) as db:
                    await _migrate_explore_id(db)
                    async with db.execute(
                        "SELECT ID, 作品标题, 作者昵称, 作者ID, 作品链接, 下载时间 "
                        "FROM explore_id"
                    ) as cur:
                        rows = [tuple(r) for r in await cur.fetchall()]
            except Exception:
                rows = []
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["作品ID", "作品标题", "作者昵称", "作者ID", "作品链接", "下载时间"])
        for rid, title, author, author_id, link, time_ in rows:
            writer.writerow([
                rid, title or "", author or "", author_id or "", link or "", time_ or ""
            ])
        content = "\ufeff" + buf.getvalue()
        filename = f"xhs_download_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ---------- 媒体代理 ----------
    @app.get("/api/image")
    async def proxy_image(url: str):
        return await proxy_media(url, "image")

    @app.get("/api/video")
    async def proxy_video(url: str, request: Request):
        return await proxy_media(url, "video", request.headers.get("range"))

    # ---------- 前端页面 ----------
    vendor_dir = WEB_DIR.joinpath("vendor")
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=vendor_dir), name="vendor")

    @app.get("/", include_in_schema=False)
    async def index():
        response = FileResponse(WEB_DIR.joinpath("index.html"))
        # 禁止缓存，避免部署更新后浏览器仍显示旧页面（配合 ETag，内容未变时仍走 304）
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(WEB_DIR.parent.joinpath("XHS-Downloader.ico"))

    @app.get("/app-icon.png", include_in_schema=False)
    async def app_icon():
        return FileResponse(WEB_DIR.parent.joinpath("XHS-Downloader.png"), media_type="image/png")

    return app
