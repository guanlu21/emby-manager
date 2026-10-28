# 这个 Dockerfile 本身不绑定架构：
# 基础镜像 python:3.11-slim 同时提供 linux/amd64 与 linux/arm64，
# 所以在 x86（拾光坞 G2）和 arm64（拾光坞 N3）上构建/运行都可以。
#
# - 在 NAS 面板上"从源码构建"：构建出来的就是该 NAS 自己的架构，天然匹配。
# - 用 GitHub Actions 构建：会同时产出 amd64 + arm64，拉取时docker 自动选对。
FROM python:3.11-slim

LABEL org.opencontainers.image.title="emby-manager" \
      org.opencontainers.image.description="Emby 用户管理面板（支持 amd64 / arm64）" \
      org.opencontainers.image.version="1.2.0"

# 时区设成上海，否则操作日志和到期时间显示会差 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据卷：数据库和备份都在这里，重建容器不丢
RUN mkdir -p /app/data && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
VOLUME ["/app/data"]

EXPOSE 8000

# 健康检查：容器起来后能自己报状态，NAS 面板上能看到是否正常
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]