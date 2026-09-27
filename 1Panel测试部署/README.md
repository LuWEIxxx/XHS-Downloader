# 1Panel 测试部署

这个文件夹只保存 1Panel 测试部署材料。项目源码仍在上一级目录；Compose 的 `build.context: ..` 会从上一级目录构建当前项目。

本方案用于先验证镜像构建、登录、设置保存、目录保存和媒体预览。它使用独立容器名、端口和测试数据目录，不复用原有 `xhs-downloader` 容器或配置卷。

> 1Panel 创建编排时可能在执行 Compose 前预先拉取所有 `image:`。因此本测试 Compose 只保留 `build:`，不声明本地镜像名。若加入 `image: xhs-downloader:security-test`，1Panel 会把它当成远程仓库并报 `pull access denied`。

## 一、准备目录

建议在 1Panel 主机上建立如下目录，并把整个项目上传进去：

```text
/opt/1panel/apps/xhs-downloader/
├── Dockerfile
├── main.py
├── webui.py
├── web_security.py
├── source/
├── static/
└── 1Panel测试部署/
    ├── docker-compose.yml
    ├── .env.example
    └── README.md
```

不要只上传 `1Panel测试部署` 文件夹；Compose 需要上一级源码目录参与构建。

在 1Panel 文件管理器中上传项目后，复制环境变量模板：

```bash
cd /opt/1panel/apps/xhs-downloader/1Panel测试部署
cp .env.example .env
mkdir -p data/volume data/downloads
```

然后编辑 `.env`，把 `XHS_WEB_PASSWORD` 改为只用于测试、且至少 16 个字符的密码。不要使用小红书 Cookie、1Panel 密码或其他线上服务密码。`.env` 不要提交或上传到公开仓库。

Live Photo 的原始 JPEG/MP4 和 v2 转换中间文件默认使用容器 RAM 临时盘：

```text
tmpfs 上限：256 MB
应用保护线：128 MiB
容器路径：/app/LivePhotoTmp
```

如果 N1 可用内存较小，可以在 `.env` 中同时调低两个值，例如：

```dotenv
XHS_LIVE_PHOTO_TMP_SIZE=128m
XHS_LIVE_PHOTO_TMP_MAX_BYTES=67108864
```

应用保护线应小于 tmpfs 上限，并且修改后需要重建/重新创建容器才能生效。

## 二、在 1Panel 创建编排

1. 确认 1Panel 当前节点已经安装并启动 Docker。
2. 进入「容器」→「编排」→「创建编排」。1Panel 支持通过编辑 Compose 内容或选择已有 `docker-compose.yml` 创建编排；本次选择文件路径即可。
3. 选择 `/opt/1panel/apps/xhs-downloader/1Panel测试部署/docker-compose.yml`。
4. 编排名称可填 `xhs-downloader-1panel-test`。
5. 如果页面提供环境变量文件选项，选择同目录的 `.env`；如果启动时提示密码未设置，检查 `.env` 是否与 `docker-compose.yml` 在同一目录。
6. 如果页面有「强制拉取镜像」选项，关闭该选项。本项目需要从上传的 Dockerfile 构建，不是从镜像仓库下载成品镜像。
7. 点击创建并启动。首次会从 Dockerfile 构建镜像，构建过程仍需拉取公开的 `python:3.12-bullseye` 和 `python:3.12-slim` 基础镜像。

不同 1Panel 版本的按钮名称可能略有差异，核心入口是「容器 → 编排」，不要使用「应用商店」安装。本文件使用独立 Compose，便于删除测试实例而不影响其他应用。

## 三、访问和首次验证

在局域网浏览器打开：

```text
http://1Panel主机IP:15556/
```

浏览器弹出登录框时，输入 `.env` 中的账号和密码。

依次验证：

1. 未登录直接访问 `/api/settings` 应返回 `401`。
2. 登录后打开设置，Cookie 输入框应为空；页面只能显示是否已配置，不能返回原 Cookie。
3. 保存一个非敏感设置，刷新页面后仍能看到设置生效。
4. 在设置中把保存根路径设为 `/downloads`，保存后应在「下载目录」中看到容器内的 `/downloads`。
5. 粘贴公开的小红书链接，解析后确认图片或视频预览可以加载。
6. 在 1Panel「容器 → 编排 → 详情 → 日志」检查没有持续重启或密码配置错误。

媒体代理只允许校验过的小红书 CDN 地址。测试环境中不要为了兼容第三方地址而把白名单改成任意 URL。

## 四、常用操作

查看日志：进入「容器」→「编排」，打开 `xhs-downloader-1panel-test` 的详情，再查看容器日志。

源码或前端修改后必须重建镜像：

```bash
cd /opt/1panel/apps/xhs-downloader/1Panel测试部署
docker compose --env-file .env up -d --build
```

只重启容器不会重新执行 Dockerfile。停止测试：

```bash
docker compose --env-file .env down
```

这会删除测试容器和网络，但保留 `data/volume` 与 `data/downloads`。不要使用 `down -v`。

## 五、端口和反向代理

测试端口映射为 `15556:5556`。如果 15556 被占用，修改左侧端口，例如 `25556:5556`，然后用新端口访问；不要修改右侧的 `5556`。

本次代码使用 HTTP Basic 登录。局域网临时测试可以直接使用 HTTP；如果通过公网或反向代理访问，应配置 HTTPS，并确保反向代理保留 `Authorization` 请求头，不要开放任意来源 CORS。

## 六、故障排查

`pull access denied for xhs-downloader`：当前使用的是旧版 Compose，其中含有 `image: xhs-downloader:security-test`。用本文件夹中更新后的 Compose 覆盖 1Panel 编排内容，确认该行已经删除，并关闭「强制拉取镜像」，然后重新创建编排。不要通过配置镜像加速器解决这个错误，因为 `xhs-downloader:security-test` 本来就不是远程仓库镜像。

`请在 .env 中设置至少16字符的测试密码`：确认 `.env` 存在、变量名准确为 `XHS_WEB_PASSWORD`，且密码长度至少 16 个字符。

`build context outside ...`：确认 Compose 文件仍位于项目根目录的 `1Panel测试部署` 文件夹，并且源码文件在它的上一级目录；不要把 Compose 文件单独移动到其他目录后再使用 `context: ..`。

页面打不开：检查 1Panel 防火墙是否放行 TCP 15556、容器是否运行，再查看日志中的构建错误或端口占用。

页面能打开但保存失败：确认浏览器使用登录框中的账号密码，并使用本次源码对应的 `static/web/index.html`，不要混用旧版本静态文件。

下载目录为空：确认主机上的 `1Panel测试部署/data/downloads` 已创建且可写；容器内路径应使用 `/downloads`。

## 七、清理测试数据

确认不再需要测试下载文件和记录后，再删除以下目录：

```text
/opt/1panel/apps/xhs-downloader/1Panel测试部署/data/volume
/opt/1panel/apps/xhs-downloader/1Panel测试部署/data/downloads
```

这会删除测试配置、下载记录和测试下载文件，属于不可恢复的清理操作。

参考：[1Panel 编排文档](https://1panel.cn/docs/v1/user_manual/containers/compose/)
