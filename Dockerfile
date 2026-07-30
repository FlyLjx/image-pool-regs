FROM node:22-bookworm-slim AS node-runtime

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REG_DATA_DIR=/app/data

WORKDIR /app

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

COPY web ./web
COPY tools ./tools
COPY data ./data

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
