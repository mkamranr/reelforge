# API and the short pipeline stages: ingest, content, audio, align, packaging.
# The renderer has its own image; this one needs no fonts and no x264.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg is needed here too: the audio stage decodes and joins synthesized
# phrases, and align.py shells out to it. The vendored video/bin binaries are
# macOS x86_64 and will not run here -- align.py's own tool() falls back to PATH.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# PyPI reads time out on slow links often enough to fail a build for no reason
RUN pip install --retries 6 --timeout 120 -r requirements.txt

COPY app/ ./app/
COPY config.yaml ./config.yaml
# The untouched original pipeline. Mounted read-only in compose; copied here so
# the image also stands alone.
COPY video/ ./video/

RUN useradd --create-home --uid 10001 reelforge \
    && mkdir -p /app/data/jobs && chown -R reelforge:reelforge /app/data
USER reelforge

EXPOSE 8020
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://localhost:8020/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]
