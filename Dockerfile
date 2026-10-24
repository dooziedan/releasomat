FROM python:3.14-slim

# Container settings (ADR 0002). Change them with env vars, e.g. in the Unraid template.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONFIG_DIR=/config \
    MUSIC_DIR=/music \
    SLSKD_DOWNLOADS_DIR=/slskd \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    TZ=UTC \
    LOG_LEVEL=INFO \
    DENO_NO_UPDATE_CHECK=1

WORKDIR /opt/releasomat

# ffmpeg: yt-dlp puts downloaded audio into its usual container with it (no re-encoding).
# tzdata: time zones for TZ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python, yt-dlp (+ yt-dlp-ejs), Deno and ytmusicapi all come in with the package: nothing from the
# host at runtime (ADR 0007). The check fails the build if YouTube support would be incomplete.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY app ./app
RUN pip install . \
    && deno --version \
    && python -c "import yt_dlp, yt_dlp_ejs, ytmusicapi; print('yt-dlp', yt_dlp.version.__version__)"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config /music

EXPOSE 8430
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8430/health', timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
# Open live-update streams would otherwise hold up "docker stop" (app/events.py).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8430", "--proxy-headers", "--timeout-graceful-shutdown", "5"]
