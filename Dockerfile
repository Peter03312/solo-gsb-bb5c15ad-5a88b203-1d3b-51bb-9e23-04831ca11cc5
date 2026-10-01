FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FLOOD_DB_PATH=/data/flood.db

WORKDIR /app

# 依赖单独一层，利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
RUN chmod +x scripts/*.sh 2>/dev/null || true

# SQLite 数据目录（compose 中以命名卷持久化）
RUN mkdir -p /data && adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# 应用启动时自动建表并（默认）初始化演示数据，无需额外迁移步骤
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
