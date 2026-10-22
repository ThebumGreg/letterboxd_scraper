#!/bin/sh
# Starts SyncHub as the user that should own /data. Docker creates a missing host folder as root, and Unraid's own user is 99:100 (nobody:users),
# so when started as root this makes /data belong to PUID:PGID and then drops to that user. Started as any other user (docker run --user, or
# "user:" in compose), it just runs the command and /data must already be writable by that user.
set -e
DATA="${SYNCHUB_DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-99}"
    PGID="${PGID:-100}"
    mkdir -p "$DATA"
    chown -R "$PUID:$PGID" "$DATA"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
if [ ! -w "$DATA" ]; then
    echo "SyncHub cannot write to $DATA as user $(id -u):$(id -g). Make the folder writable for that user, or start the container as root and set PUID and PGID." >&2
    exit 1
fi
exec "$@"
