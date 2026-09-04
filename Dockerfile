FROM python:3.12-slim

WORKDIR /app

# 换阿里云 Debian 镜像源：官方源在国内服务器会长时间挂起（实测 apt 卡死 30 分钟无进展）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list; \
    fi

# 中文字体（电子贺卡 Pillow 渲染需要）
# 用 wqy-microhei（约 5MB）而非 fonts-noto-cjk（300MB+，构建极易超时），
# 字体探测路径 agent/skills/skill_greeting.py::_FALLBACK_FONT_PATHS 已内置该路径。
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt

# 复制代码
COPY . .

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
