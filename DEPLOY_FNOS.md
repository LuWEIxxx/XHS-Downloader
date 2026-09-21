# XHS-Downloader Web 版 · 飞牛 NAS 部署指南

本项目在开源项目 [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) 基础上改造：
- **后端**：复用项目原有 FastAPI 与下载核心，新增 Web 管理接口
- **前端**：浏览器页面（粘贴链接 → 解析预览 → 勾选多选 → 下载）
- **部署**：Docker 容器，支持飞牛 fnOS 一键导入

---

## 一、文件清单

| 文件 | 说明 |
| --- | --- |
| `Dockerfile` | 镜像构建（Web 模式启动） |
| `docker-compose.yml` | Compose 编排（端口、卷、环境变量） |
| `webui.py` | Web 后端扩展（API 接口 + 静态页面托管） |
| `static/web/` | 前端页面（含本地托管的 Vue3，无需外网） |
| `main.py` | 入口（新增 `python main.py web` 模式） |

---

## 二、部署步骤（飞牛 fnOS）

### 1. 上传项目

把整个项目目录（含 `Dockerfile`、`docker-compose.yml`、`source/`、`static/`、`main.py`、`webui.py`）上传到飞牛 NAS 的任意目录，例如 `/vol1/1000/软件/xhs-downloader/`。

### 2. 修改 Compose 中的下载目录

编辑 `docker-compose.yml`，把这一行改成你 NAS 上的实际共享目录（下载文件将保存到这里）：

```yaml
- /vol1/1000/Download:/downloads
```

例如你的照片/下载共享文件夹路径是 `/vol1/1000/Download`，就这样写。挂载进容器后，前端「设置 → 保存根路径」选择 `/downloads` 即可。

> 💡 **自动探测**：只要容器内存在 `/downloads`（或 `/data`、`/download`）目录，程序首次启动会自动把「保存根路径」设为该目录并写入配置，**无需手动设置**，下载文件直接落在 NAS 共享目录里。
>
> ⚠️ **已部署的老容器**：如果之前已经下载过文件（存到了 docker 卷里），升级代码后需**重建容器**（Compose 面板点「重建」）才会触发自动探测；旧文件不会自动迁移，可手动拷贝：
> ```bash
> # 把卷里的旧文件复制到 NAS 共享目录（在 NAS 终端执行）
> docker cp xhs-downloader:/app/Volume/Download/. /vol1/1000/Download/
> ```

### 3. 创建 Compose 项目

1. 打开飞牛 NAS 的 **Docker** 应用 → 左侧 **Compose** → **新建项目**
2. 项目名称随意（如 `xhs-downloader`）
3. 路径选择第 1 步上传的项目目录（会自动识别 `docker-compose.yml`）
4. 点击 **创建/启动**，首次会自动构建镜像（约 3~8 分钟）

### 4. 访问

浏览器打开 `http://NAS的IP:5556`，即可使用 Web 界面。

> 如 NAS 有防火墙，请放行 5556 端口；仅建议在局域网内使用。

---

## 三、使用说明

### 下载流程
1. 粘贴小红书作品链接（支持多个，空格/换行分隔，支持 `xhslink.com` 短链）
2. 点击「解析链接」→ 预览作品（图片网格 / 视频播放器）
3. 勾选要下载的图片（支持多选，动图会标记「动图」角标）
4. 点击「下载所选」→ 文件保存到设置的目录

### 设置项
设置面板覆盖开源项目全部配置：
- **保存路径**：保存根路径（浏览按钮可可视化选择容器内目录）+ 下载文件夹名称
- **Cookie**：非必填；配置后可下载视频高清画质
- **代理 / UA / 超时 / 重试**：网络相关
- **图文格式**（AUTO/PNG/WEBP/JPEG/HEIC）、**视频偏好**（分辨率/码率/大小）
- **功能开关**：图文/视频/动图下载、单作品独立文件夹、按作者归档、下载历史记录、写入修改时间等

设置保存后立即生效（后端自动重建实例），无需重启容器。

---

## 四、其他部署方式

### 命令行构建 + 运行

```bash
docker build -t xhs-downloader:web .
docker run -d --name xhs-downloader \
  -p 5556:5556 \
  -v xhs_config:/app/Volume \
  -v /vol1/1000/Download:/downloads \
  -e TZ=Asia/Shanghai \
  --restart unless-stopped \
  xhs-downloader:web
```

### 切换运行模式

默认 Web 模式。如需 TUI / API / MCP 模式，修改 `docker-compose.yml` 中的 `command`（见文件内注释），或 docker run 时追加 `python main.py api`。

---

## 五、常见问题

### ⚠️ 构建失败：`401 Unauthorized`（镜像加速器失效）—— 最常见

**报错特征**（构建一开始就失败，还没到项目代码）：
```
[internal] load metadata for docker.io/library/python:3.12-bullseye
ERROR: unexpected status from HEAD request to https://docker.fnnas.com/v2/library/python/manifests/3.12-bullseye: 401 Unauthorized
failed to solve: python:3.12-bullseye: failed to resolve source metadata ...
```

**原因**：飞牛 NAS 预置的镜像加速器 `docker.fnnas.com` 已失效（返回 401），导致连基础镜像都拉不下来。这与项目代码无关。

**解决方案（任选其一，推荐方案 A）：**

**方案 A：更换镜像加速器（推荐，改完一劳永逸）**
1. 飞牛 NAS → Docker → **设置 → 镜像加速**（registry mirrors）
2. 删除失效的 `docker.fnnas.com`，填入以下可用源（2026 年实测，可多填几个做容灾）：
   ```
   https://docker.1ms.run
   https://docker.1panel.live
   https://docker.m.daocloud.io
   ```
   ⚠️ 清华 TUNA / 中科大 / 网易 163 的 Docker 加速已停用，不要填
3. 保存后，先在 NAS 终端验证能拉基础镜像，再重新构建：
   ```bash
   docker pull python:3.12-slim
   ```
   拉取成功即说明加速器已生效，回到 Compose 重新「创建/构建」。

**方案 B：SSH 修改 Docker 配置**（UI 没有加速设置项时）
```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.1panel.live",
    "https://docker.m.daocloud.io"
  ]
}
EOF
sudo systemctl restart docker
docker pull python:3.12-slim   # 验证
```

**方案 C：不改全局配置，先手动拉基础镜像打 tag（最快兜底）**
在 NAS 终端执行：
```bash
docker pull docker.1ms.run/library/python:3.12-slim
docker tag  docker.1ms.run/library/python:3.12-slim python:3.12-slim
docker pull docker.1ms.run/library/python:3.12-bullseye
docker tag  docker.1ms.run/library/python:3.12-bullseye python:3.12-bullseye
```
本地有这两个 tag 后，重新构建会直接用本地镜像，不再访问网络拉基础镜像。

> 以上加速地址为社区公共公益服务，如某个失效，换列表里其他地址重试即可。

---

| 问题 | 处理 |
| --- | --- |
| 解析失败/风控 | 在设置中填入小红书网页版 Cookie（获取方法：浏览器 F12 → 网络 → 任意请求 → 复制 Cookie 头） |
| 图片预览加载失败 | 预览走后端代理，检查容器能否访问外网；若使用代理工具，在设置中配置 proxy |
| 下载的文件在哪 | 默认 `/app/Volume/Download`（配置卷内）；建议在设置中选择 `/downloads` 下的 NAS 路径 |
| 重复下载被跳过 | 这是「下载历史记录」功能（默认开启），可在设置中关闭，或接受跳过 |
| ARM 架构 NAS | 需要 `docker buildx build --platform linux/arm64` 构建对应架构镜像 |

---

## 六、二次开发

- Web 后端接口见 `webui.py`，核心逻辑复用 `source/application/app.py` 的 `XHS` 类
- 前端为单页 `static/web/index.html`（Vue3 本地托管），修改后无需构建，刷新即可
- 原项目的 API 模式接口 `/xhs/detail` 依然可用（Web 服务已内置兼容）
