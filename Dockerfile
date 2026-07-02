# SENTINEL Protocol — slim container image.
#
# Recommended build (from this directory, i.e. sentinel/):
#   docker build -t sentinel:latest .
# or:
#   docker compose -f docker-compose.yml up --build
#
# The first attempt used the regular `xgboost` package, which on PyPI now
# depends on the full nvidia CUDA stack (~400 MB on a CPU-only image). We
# switch to `xgboost-cpu`, which is the same module/API but ships without
# any nvidia-* transitive dependencies. Combined with single-stage slim
# base, the resulting image is ~700 MB (down from 2.2 GB).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    PYTHONPATH=/app

# Runtime system deps. libgomp1 is required by xgboost at runtime, curl is
# for the health check, tini reaps zombies and forwards signals to uvicorn.
# We do all installs in one layer and clean apt cache in the same layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching — only rebuilt when
# requirements.txt changes).
COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    # Defensive: if a transitive dep ever pulls in nvidia-* libs (none
    # should with xgboost-cpu), strip them — they're useless on a CPU
    # image and would balloon the size.
    && (pip uninstall -y nvidia-nccl-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cusolver-cu12 nvidia-cusparse-cu12 nvidia-nvjitlink-cu12 nvidia-nvtx-cu12 2>/dev/null || true) \
    && find /usr/local/lib/python3.11 -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.11 -name '*.pyc' -delete \
    && find /usr/local/lib/python3.11 -name '*.so' -exec strip --strip-unneeded {} + 2>/dev/null || true

# Copy the rest of the package (sentinel/*.py + sentinel/static + sentinel/ml
# + sentinel/.env.example). The build context is this directory (sentinel/).
COPY . ./sentinel/

# Drop privileges. Fixed uid matches common containerd/cri defaults and
# avoids running as root.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Lightweight health check against /api/health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/api/health || exit 1

# tini reaps zombies and forwards signals to uvicorn.
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "uvicorn sentinel.main:app --host 0.0.0.0 --port ${PORT}"]
