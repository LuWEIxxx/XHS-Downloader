# ---- 阶段 1: 构建器 (Builder) ----
# 使用一个功能完整的镜像，它包含编译工具或可以轻松安装它们
FROM python:3.12-bullseye AS builder

# 切换为清华源（国内构建加速，官方源经常超时）
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list

# 安装编译 uvloop 和 httptools 所需的系统依赖 (C编译器 等)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制需求文件
COPY requirements.txt .

# 在这个具备编译环境的阶段安装所有 Python 依赖
# 使用清华源安装（国内 pypi.org 经常超时或极慢）
RUN pip install --no-cache-dir --prefix="/install" \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

# ---- 阶段 2: 最终镜像 (Final Image) ----
# 使用轻量级 slim 镜像作为最终的运行环境
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 添加元数据标签
LABEL name="XHS-Downloader" authors="JoeanAmier" repository="https://github.com/JoeanAmier/XHS-Downloader"

# 从构建器阶段，将已经安装好的依赖包复制到最终镜像的系统路径中
COPY --from=builder /install /usr/local

# 复制项目代码和相关文件（含 Web 前端页面）
COPY locale /app/locale
COPY source /app/source
COPY static /app/static
COPY LICENSE /app/LICENSE
COPY main.py /app/main.py
COPY webui.py /app/webui.py

# 创建挂载点（配置、下载记录、下载文件均在此持久化）
VOLUME /app/Volume

# 暴露端口
EXPOSE 5556

# 默认启动 Web 模式（前端页面 + API）
# 其他模式：TUI -> python main.py ; API -> python main.py api ; MCP -> python main.py mcp
CMD ["python", "main.py", "web"]
