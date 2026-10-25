# SyncHub, the main app. Build from the repository folder:   docker build -t synchub .
# Everything worth keeping (database, logs, poster cache, backups) lives in the one volume /data.
FROM python:3.12-slim

# (The base image already has tzdata, so TZ=Europe/London and the like work: the nightly backup runs at 03:30 in that zone.)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    TZ=UTC \
    PUID=99 \
    PGID=100 \
    SYNCHUB_DATA_DIR=/data \
    SYNCHUB_IN_DOCKER=1

# adb: the Google TV / Android TV pages open apps and read what is playing through it (the TV pairing key it makes is kept under /data/tv).
# ping: the TV tools' network probe. Both are small.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends adb iputils-ping || apt-get install -y --no-install-recommends android-tools-adb iputils-ping) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="SyncHub" \
      org.opencontainers.image.description="Letterboxd/Serializd, Jellyfin, Radarr and Sonarr in one place, with a whole-house TV remote" \
      org.opencontainers.image.source="https://github.com/ThebumGreg/letterboxd_scraper" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

COPY config.py logger.py settings_validation.py ./
COPY synchub ./synchub
COPY static ./static
COPY docker/entrypoint.sh /entrypoint.sh
# Whatever the code travelled through (Windows line endings, a copy that lost the executable bit), the script must run.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod 0755 /entrypoint.sh

VOLUME /data
EXPOSE 5001

# Up, and the database answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5001/healthz', timeout=4).status == 200 else 1)"

# ONE worker process: the scheduler runs inside it, and a second process would run every job twice. Threads serve the pages and the live dashboard streams.
STOPSIGNAL SIGTERM
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--worker-class", "gthread", "--threads", "16", "--timeout", "120", "--graceful-timeout", "30", "synchub.web:create_app(start_scheduler=True)"]
