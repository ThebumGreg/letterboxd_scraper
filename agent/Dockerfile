# The SyncHub worker agent. Build it from the folder that holds this file:  docker compose up -d --build
# Debian 13 (trixie): its ffmpeg 7 is built with libvmaf (quality scores), VAAPI and QSV. The older Debian 12 ffmpeg had no libvmaf.
FROM python:3.12-slim-trixie

# ffmpeg and ffprobe: library scans use them to read and check files, and conversions use ffmpeg. Copy jobs need neither.
# intel-media-va-driver and vainfo are for Intel GPU encoding (VAAPI/QSV): they only do something when the container is given /dev/dri
# (see examples/docker-compose.gpu.yml). The build stops if this ffmpeg has no libvmaf, so a missing quality check is never a silent surprise.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg intel-media-va-driver vainfo \
    && ffmpeg -hide_banner -filters | grep -q libvmaf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY synchub_agent.py media_analysis.py /app/

# /work holds the job folders, the original files kept for the retention period and the logs: mount a big local disk there.
ENV PYTHONUNBUFFERED=1 \
    SYNCHUB_WORK=/work
VOLUME /work

# Docker's stop sends SIGTERM; the agent stops at the next safe point and unfinished jobs resume when it starts again.
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD ["python", "/app/synchub_agent.py", "--healthcheck"]

# Arguments after the image name go straight to the agent, for example:  docker compose run --rm worker --check
ENTRYPOINT ["python", "/app/synchub_agent.py"]
