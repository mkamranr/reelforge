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

# Not `USER reelforge`: the entrypoint starts as root, makes the bind-mounted
# /app/data writable, and drops privileges itself. A bind mount replaces the
# directory this image just chowned, so the build-time ownership above only
# helps when /app/data is NOT mounted over -- which is the standalone case.
COPY docker/entrypoint.sh /entrypoint.sh
# chmod here rather than trusting the checkout: a clone on a filesystem
# that drops the executable bit would fail at start with "permission denied"
# and nothing else to go on.
RUN chmod 0755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

EXPOSE 8020
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://localhost:8020/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]
