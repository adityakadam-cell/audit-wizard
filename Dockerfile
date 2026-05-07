# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000

RUN useradd --create-home --shell /bin/bash app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=app:app . .
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz').read()" || exit 1

CMD ["sh", "-c", "python -m gunicorn wsgi:app --workers 1 --threads 4 --bind 0.0.0.0:${PORT} --timeout 200 --access-logfile -"]
