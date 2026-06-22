# Miraje — container image
# Minimal Python image, no build step, runs as a non-root user.

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt watchfiles

# Copy application code and static assets.
COPY app/ ./app/
COPY static/ ./static/

# Persistent local data (SQLite, etc.)
RUN mkdir -p /app/data && \
    useradd --create-home --uid 1000 miraje && \
    chown -R miraje:miraje /app

USER miraje

EXPOSE 8000

# Sensible defaults; override with -e flags or .env.
ENV MIRAJE_HOST=0.0.0.0 \
    MIRAJE_PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3); sys.exit(0)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
