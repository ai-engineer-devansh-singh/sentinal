# SENTINEL Protocol — containerized image.
#
# Recommended build (from this directory, i.e. sentinel/):
#   docker build -t sentinel:latest .
# or:
#   docker compose -f docker-compose.yml up --build
#
# The app is run as a module: `python -m sentinel.main`, so the image layout
# keeps `sentinel/` as a top-level package directory inside `/app`.

FROM python:3.11-slim AS base

# System deps: build-essential is only needed for wheels that lack a musl
# build (e.g. some xgboost/scikit-learn wheels historically); curl is for the
# health check. Clean apt cache to keep the image small.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching — only rebuilt when
# requirements.txt changes).
COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy the rest of the package (sentinel/*.py + sentinel/static + sentinel/ml
# + sentinel/.env.example). The build context is this directory (sentinel/),
# and the package files live alongside this Dockerfile.
COPY . ./sentinel/

# Drop privileges. `app` is created with a fixed uid to match typical
# containerd/cri defaults and avoid running as root.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Lightweight health check: hit /api/health. uvicorn returns 200 with JSON
# describing grok_enabled / speed / paused.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/api/health || exit 1

# Run the FastAPI app via uvicorn. /app is on PYTHONPATH so
# `sentinel.main:app` resolves correctly.
ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn sentinel.main:app --host 0.0.0.0 --port ${PORT}"]
