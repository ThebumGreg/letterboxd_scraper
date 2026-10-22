# The SyncHub worker agent. Build it from the folder that holds this file:  docker compose up -d --build
FROM python:3.12-slim

# ffmpeg is what the encoding jobs (a later phase) use; copy jobs do not need it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY synchub_agent.py /app/synchub_agent.py

# /work holds the job folders, the original files kept for the retention period and the logs: mount a big local disk there.
ENV PYTHONUNBUFFERED=1 \
    SYNCHUB_WORK=/work
VOLUME /work

# Docker's stop sends SIGTERM; the agent stops at the next safe point and unfinished jobs resume when it starts again.
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD ["python", "/app/synchub_agent.py", "--healthcheck"]

# Arguments after the image name go straight to the agent, for example:  docker compose run --rm worker --check
ENTRYPOINT ["python", "/app/synchub_agent.py"]
