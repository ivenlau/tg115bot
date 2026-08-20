FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 构建依赖（部分 native 扩展需要 gcc）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p config downloads db logs

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python scripts/healthcheck.py || exit 1

CMD ["python", "main.py"]
