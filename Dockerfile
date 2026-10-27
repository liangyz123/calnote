# ============================================================
# 日历便签 (calnote) —— Docker 镜像
# 构建：docker build -t calnote .
# 运行：docker run -d --name calnote -p 8000:8000 -v ./data:/app/data calnote
# ============================================================
# 用国内可访问的镜像源（Docker Hub 直连经常超时）
FROM docker.m.daocloud.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# v6.2c：不再执行任何 apt 安装（Pillow 的 wheel 自带图片解码库，构建只走 pip 清华源，一两分钟内完成）
# 想给视频抽一帧当封面：再手动加一行 RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
#   （建议先跑一遍 sed 换国内 apt 源，否则会像刚才那样卡很久）



# 
# 先装依赖（利用 Docker 层缓存，改代码不用重新下载依赖）
# pip 用清华源，避免 PyPI 下载慢
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制程序
COPY app.py .
COPY ledger.py .
COPY vault.py .
COPY static ./static
COPY tools ./tools

# 数据目录（SQLite 数据库放在这里，部署时挂载到 NAS 磁盘）
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/calnote.db \
    PORT=8000

EXPOSE 8000
VOLUME ["/app/data"]

# 健康检查（飞牛/Docker 界面能看到“健康”状态）
HEALTHCHECK --interval=60s --timeout=6s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)" || exit 1

CMD ["python", "app.py"]
