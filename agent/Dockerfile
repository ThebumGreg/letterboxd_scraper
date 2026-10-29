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

# mkvtoolnix (mkvmerge) and dovi_tool convert Dolby Vision profile 7 to 8.1 without re-encoding the picture (the "dovi" feature). dovi_tool is a prebuilt release
# checked against a fixed checksum. If it cannot be downloaded the image is still built, the worker just does not offer that job (see --check).
ARG DOVI_TOOL_VERSION=2.3.0
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix curl ca-certificates \
    && ( case "${TARGETARCH:-amd64}" in \
           amd64) arch=x86_64; sum=6aa166dbdcedb5c09bac99b832aa0013f6bc95aeb6db44d62d79b41c8abf0d98 ;; \
           arm64) arch=aarch64; sum=fc8523b84e61153c4c434cfabf23c3490056d54d0789d029173b36368b662eb2 ;; \
           *) echo "dovi_tool: no build for ${TARGETARCH}"; exit 1 ;; \
         esac \
         && curl -fsSL -o /tmp/dovi_tool.tgz "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-${arch}-unknown-linux-musl.tar.gz" \
         && echo "${sum}  /tmp/dovi_tool.tgz" | sha256sum -c - \
         && tar -xzf /tmp/dovi_tool.tgz -C /usr/local/bin ./dovi_tool \
         && dovi_tool --version \
       || echo "WARNING: dovi_tool was not installed; Dolby Vision conversion will not be offered by this worker" ) \
    && rm -f /tmp/dovi_tool.tgz \
    && apt-get purge -y curl && apt-get autoremove -y \
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
