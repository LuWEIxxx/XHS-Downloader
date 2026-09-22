# Web 登录与媒体代理

本次仅加固 `python main.py web` 的鉴权、Cookie 设置和媒体代理。

## 部署与登录

1. 将 `.env.example` 复制为 Compose 同目录的 `.env`。
2. 设置 `XHS_WEB_PASSWORD` 为至少 16 字符的独立随机密码；`XHS_WEB_USERNAME` 默认 `admin`。
3. 重建并启动：`docker compose up -d --build`。
4. 打开页面后，在浏览器原生登录框输入上述账号和密码。

未配置密码或密码过短时拒绝启动。真实 `.env` 已加入 Git 忽略列表。直接运行 Python 时需在启动进程的环境变量中设置这两个值，程序不会自行读取 `.env`。

HTTP Basic 不加密凭据，正式部署应经 HTTPS 反向代理访问；不要通过公网明文 HTTP 登录。反向代理需保留 Authorization 请求头，且不要配置允许任意来源的 CORS。容器端口即使被直接访问也需要应用自己的账号密码。浏览器会缓存 Basic 凭据；更换账户可使用新的隐私窗口，修改环境变量并重建容器后旧密码立即失效。

页面、静态资源、OpenAPI 文档及全部 `/api/*`（包括媒体、目录、记录和导出）统一认证。修改操作还要求 `X-XHS-Request: 1`，前端已自动发送；跨站浏览器请求被拒绝。API 客户端需使用 Basic 认证，并在写请求附加该请求头。响应禁止缓存，避免媒体或设置留在共享缓存中。

Web 模式禁止启动原有未鉴权的 5558 用户脚本服务，旧配置中的开关不在 Web 模式生效。独立 TUI/API/MCP 模式不在本次改动范围内，不能视为已获得同样的鉴权保护。

## Cookie 设置

读取及保存设置的响应均不返回 Cookie，只返回 `settings.cookie_configured`。新 Cookie 通过 PUT `/api/settings` 的独立 `cookie` 字段提交；省略、空串或空白均保留旧值；`clear_cookie: true` 明确清除。不允许同时清除和设置非空 Cookie。旧式 `settings.cookie` 不再用于更新，避免空表单误删凭据。

## 媒体边界

- 仅允许 `xhscdn.com` 子域名，以及源码默认图片格式使用的确切域名 `ci.xiaohongshu.com`，端口限标准 HTTP/HTTPS；拒绝 URL 用户信息、控制字符、片段和伪造域名。
- 每一跳解析 DNS，全部解析结果必须为公网单播 IP，拒绝内网、回环、链路本地、保留地址及常见 IPv6 转换地址。
- 将连接目标固定为已检查的 IP，保留原始 Host 和 HTTPS SNI/证书验证，避免第二次域名解析造成 DNS 重绑定。最多跟随三次重定向，每一跳重新校验。
- 独立客户端不携带管理认证、小红书 Cookie 或上游 Set-Cookie；不读取系统代理环境或下载器代理配置，以保证实际连接目标经过检查。
- 图片仅接受 JPEG/PNG/WebP/GIF/AVIF/HEIC/HEIF，视频仅接受 MP4/WebM/QuickTime/M4V。拒绝 HTML、SVG、未知类型和压缩响应；设置 `nosniff` 及禁用脚本的 CSP。
- 视频保留单段 Range 及 206 响应，连接在完成、错误和断开时关闭。代理连接独立于下载器设置热更新。

如果未来上游使用新 CDN 域名或媒体类型，应先核实样本再更新白名单。不要为了兼容直接放开任意地址。现有下载核心的网络请求策略不在本次修改范围内。

IP 固定及 SNI 使用 HTTPX 的官方接口：[HTTPX extensions](https://www.python-httpx.org/advanced/extensions/#sni_hostname)。

## 回归测试

在安装现有项目依赖的 Python 3.12+ 环境运行：

```text
python -m unittest discover -s tests -v
```

测试使用真实 Web 路由和隔离的上游媒体响应，不使用真实小红书 Cookie，不访问 NAS。
