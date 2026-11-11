# yt2abs runtime image: Python + yt-dlp + ffmpeg + Deno (yt-dlp JS challenges).
FROM denoland/deno:bin-2.9.8 AS deno

FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    YT2ABS_YTDLP_BIN_DIR=/data/bin \
    YT2ABS_HOST=0.0.0.0 \
    YT2ABS_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno
RUN chmod 0755 /usr/local/bin/deno && deno --version

WORKDIR /app
COPY pyproject.toml README.md config.example.yaml ./
COPY yt2abs ./yt2abs
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir --upgrade "yt-dlp[default]" \
    && python -c "import yt_dlp_ejs, mutagen; print('yt-dlp-ejs', yt_dlp_ejs.version, '| mutagen', mutagen.version)" \
    && yt-dlp --version

# Writable runtime directory for the unprivileged user (SQLite, downloads, the
# yt-dlp binary dir); bind mounts override it at run time.
RUN mkdir -p /data && chown -R 99:100 /data

# Unraid-friendly uid/gid; override with `docker run --user`.
USER 99:100

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "-m", "yt2abs"]
