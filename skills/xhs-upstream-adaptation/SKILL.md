---
name: xhs-upstream-adaptation
description: 适配 JoeanAmier/XHS-Downloader 上游更新到本项目的飞牛 fnOS 部署版本；处理 curl_cffi 请求层、下载任务结构、Live Photo、数据库兼容和 Docker 验证。仅用于本项目或结构相同的定制分支，不负责普通功能开发。
metadata:
  short-description: 小红书下载器上游更新适配
---

# XHS-Downloader 上游适配

这个项目是 `JoeanAmier/XHS-Downloader` 的定制分支，生产目标是飞牛 fnOS 上的 Docker Web 部署，N1/1Panel 只用于局域网构建和回归测试。上游更新时，目标不是把本地文件全部覆盖成上游版本，而是让上游新功能进入本项目，同时保留下面的本地不变量。

## 先读取的项目约束

- 生产编排是根目录的 `Dockerfile`、`docker-compose.yml` 和 `DEPLOY_FNOS.md`。
- `1Panel测试部署/` 是 N1 的独立测试编排，`build.context: ..` 依赖完整项目树；它不是生产配置，也不能单独上传构建。
- `Volume/` 是本地测试数据目录，包含 `ExploreID.db`、`MappingData.db`、`settings.json` 和下载记录；部署中的 `/app/Volume` 及用户指定的下载目录同样属于用户数据，禁止在适配或测试中删除、重建或用空库覆盖。
- 下载历史数据库采用当前项目的迁移策略：`source/module/recorder.py` 通过 `PRAGMA table_info` 检查缺失列并 `ALTER TABLE` 补列。不要改回上游的清空重建方案，也不要把 `ExploreID.db` 当作临时测试文件。
- 当前不承诺旧 API 兼容。适配以本项目现有 Web 接口、前端契约和测试为准；不要为了旧调用方重新引入已删除的接口，除非用户另行要求。
- 不要把 Cookie、`.env`、下载目录或数据库写入提交内容。Cookie 为空时仍应允许启动；解析失败和风控提示必须与下载转换错误分开诊断。

## 上游同步工作流

1. **建立基线。** 记录当前提交、工作区状态和测试结果；先确认 `git diff` 中哪些修改属于本项目。优先在临时分支或工作树中同步，不直接覆盖工作区。
2. **读取上游变更。** 重点审查 `pyproject.toml`/`requirements.txt`/`uv.lock`、`source/module/manager.py`、`source/application/request.py`、`source/application/download.py`、`source/module/recorder.py`、`source/module/settings.py`、`webui.py` 和静态前端。先看数据流和任务结构，再解决冲突。
3. **保留本地边界。** 重新应用数据库路径/迁移、飞牛 Compose、RAM 临时盘、Web 登录与安全代理、Live Photo 封装和本地 API 测试；不要把这些改动散落复制到上游任意位置。
4. **先做静态检查，再做干净环境测试。** 使用新的干净 Python 环境安装锁定依赖；运行 `python -m unittest discover -s tests`、`python -m compileall -q source webui.py main.py`、`uv lock --check --offline` 和 `git diff --check`。不要只复用已有虚拟环境，因为它可能掩盖依赖缺失。
5. **构建生产镜像。** 在项目根目录构建根 Compose；确认 Dockerfile 复制了新增模块和静态文件。只有生产镜像构建通过后，才用 `1Panel测试部署` 在 N1 做局域网冒烟测试。
6. **按顺序验收。** 先测试普通静态图，再测试视频，再测试含 Live Photo 的图文，最后测试 Web 登录、设置保存、下载记录和重启后的数据保留。N1 测试通过不等于飞牛生产环境已验证，生产重建必须由用户决定。

## 请求层适配要点

上游 2.8 的小红书请求已经由 `httpx` 切换为 `curl_cffi` 浏览器指纹请求。本项目的下载/解析请求必须继续遵循这一边界：

- `source/module/manager.py` 中的 `AsyncSession`、`BrowserTypeLiteral`、异常类型来自 `curl_cffi.requests`。
- `source/application/request.py` 的无 Cookie 请求使用 `manager.request_client`，有 Cookie 请求使用 `curl_cffi.requests.get(..., cookies=...)`，不要把 Cookie 拼成裸 `Cookie` 请求头。
- 下载器的异常捕获使用 `curl_cffi.requests.exceptions.RequestException`；不要只替换 import 而遗漏同步/异步 Session、`proxy`、`impersonate` 和响应流接口。
- `web_security.py` 是媒体代理的 SSRF 防护实现，目前有意使用 `httpx` 做安全代理和测试 Mock。不要执行全局搜索替换；“小红书业务请求不用 httpx”和“安全代理仍用 httpx”是两个不同边界。
- 依赖升级时同时检查 `pyproject.toml`、`requirements.txt` 和 `uv.lock`，确认运行时和 Docker 构建使用的是同一套约束。

## 下载任务结构：最容易回归的契约

`Download.run()` 和 `__ready_download_*()` 之间是位置元组契约，修改任一方必须同步修改测试：

- 普通视频或视频封面：`(url, name, format)`。
- 普通图文图片：`(image_url, name, image_format)`。
- Live Photo：`("live", image_url, live_url, name)`。

Live Photo 任务必须先判断 `task[0] == "live"`，普通图片只能读取 `task[0]`、`task[1]`、`task[2]`。曾经出现过普通图文下载报 `list index out of range`：生产者只有三项，消费者却读取 `task[3]`；这个错误发生在解析成功之后，不能误判为 Cookie 或风控。

每次改动任务结构都要有至少一个测试实际调用 `Download.run()`，而不是只测试私有生成函数。验收至少包括：空 Live URL 的普通图片、有效 Live URL 的四元组、索引下载和已存在文件跳过。

## Live Photo 与 RAM 临时空间

本项目的 `source/application/live_photo.py` 负责 v2 Apple Live Photo 封装：封面 JPEG 和视频 MOV 使用同一个 UUID，视频不重新编码，只修改容器/元数据；最终输出为同名 `.JPG` 和 `.MOV`。该模块包含来自 `yangzhen-23/video-to-live-photo` 的 Apache-2.0 参考实现，保留现有版权说明，后续改动不要删除归属信息。

下载器的 RAM 策略如下：

- Compose 将 `/app/LivePhotoTmp` 挂载为 tmpfs；`XHS_LIVE_PHOTO_TMP` 指向该路径。
- `XHS_LIVE_PHOTO_TMP_MAX_BYTES` 是应用保护线，默认 128 MiB；Compose tmpfs 默认 256 MiB。保护线必须小于 tmpfs 容量。
- `LIVE_PHOTO_SEMAPHORE` 将 Live Photo 转换串行化；不要为了吞吐量盲目并发高分辨率图片。
- 原始文件和转换中间文件先写到 RAM，目标卷使用 `.part` 文件；只在目标卷内部使用 `os.replace`，避免 tmpfs 与持久卷之间出现 `Invalid cross-device link`。
- 转换失败或超限必须清理 RAM 中的源文件和半成品，不能留下会被下次任务误识别的文件。目标目录只能在 JPG/MOV 都成功后出现最终文件。
- 若运行环境未提供 tmpfs，代码可回落到配置的临时目录，但应在部署说明中明确这会写入持久存储；不能宣称“必然在 RAM 中完成”。

Live Photo 回归验收：普通图片仍按设置的格式保存；动态图片只留下配对的 `.JPG`/`.MOV`；两者 UUID 一致；重启后数据库和下载文件仍存在；RAM 超限时不影响系统且任务可重试。

## 数据库与设置迁移

- `ExploreID.db` 的路径和字段是用户已有下载记录的兼容边界。新增字段必须采用“检测缺失列后补列”的可重复迁移；不得依赖一次性手工 SQL，更不能改变旧 ID 的含义。
- `settings.json` 通过默认值补齐新增配置。新增设置必须有安全默认值，Cookie 默认空字符串，Live Photo 默认关闭；读取旧设置后再写回时不得丢失未知字段，除非明确设计了迁移。
- 测试迁移时使用临时 SQLite 文件；不要用仓库中的 `Volume/ExploreID.db` 做 destructive fixture。需要验证真实兼容性时先复制数据库，再检查表结构、记录数和关键 ID 未变化。

## 错误定位速查

- `未解析到有效作品数据`：先检查 URL 短链展开、Cookie/指纹、风控和解析响应；它发生在作品数据层，不是 Live Photo 转换异常。
- `list index out of range`：优先检查下载任务生产者/消费者的元组长度，特别是普通三元组和 Live Photo 四元组的分支。
- `Invalid cross-device link`：检查是否把 tmpfs 中的文件直接 `os.replace` 到 `/app/Volume` 或 NAS 挂载；必须在目标卷创建 `.part` 后再替换。
- 下载成功但目录为空：检查实际 `settings.json` 的工作路径、`/downloads` 挂载、容器用户权限和是否误看了旧的默认目录；不要先删除数据库。
- 运行一段时间后内存升高：检查 tmpfs 容量、应用保护线、失败清理和 Live Photo 并发数；不要仅靠扩大 tmpfs 解决。
- 构建时缺模块或旧前端：确认 Dockerfile 的 `COPY source`/`COPY static` 覆盖了新增文件，并重建镜像而不是只重启容器。

## 最小验收清单

```text
[ ] git diff 中没有 .env、Cookie、Volume、下载文件或数据库
[ ] 普通静态图下载成功，且没有读取 task[3]
[ ] 视频下载和视频封面开关正常
[ ] Live Photo 生成 JPG + MOV，UUID 一致，失败时无半成品
[ ] RAM 上限触发时任务失败可重试，持久卷和系统不受破坏
[ ] ExploreID.db 旧库可打开，记录数/ID 不变，新增列可迁移
[ ] settings.json 旧配置补齐新字段且 Cookie 不泄漏
[ ] 业务请求使用 curl_cffi；安全媒体代理的 httpx 不被误删
[ ] 干净环境 unittest、compileall、uv lock --check、git diff --check 通过
[ ] 根目录生产 Docker 构建通过
[ ] N1 仅作为局域网冒烟平台，飞牛生产重建另行确认
```

## 推荐的长期分支策略

把本项目维护成自己的 fork：将上游仓库命名为 `upstream`，将自己的仓库命名为 `origin`；每次更新先 `git fetch upstream`，在临时分支上用 `git range-diff` 或逐提交 `cherry-pick` 复核，再合并到部署分支。把本地改动保持为少量主题明确的提交（例如“fnOS Compose/RAM”“Live Photo”“数据库迁移”“Web 安全”），不要把上游同步和本地功能混成一个不可审查的大提交。

如果未来适配次数增加，优先把本地行为放在独立模块、配置项和回归测试中，让上游核心文件只保留必要的小补丁。每次上游同步后先解决接口/依赖冲突，再执行本文件的验收清单；任何未验证的飞牛部署结论都必须标为“未测试”，不能用 N1 结果替代。
