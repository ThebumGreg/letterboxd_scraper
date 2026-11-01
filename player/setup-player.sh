#!/usr/bin/env bash
# setup-player.sh: sets up a SyncHub TV player PC (Ubuntu or Debian) with one command.
#
#   git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper
#   sudo ./player/setup-player.sh --url http://192.168.1.20:5001 --token sp_... --name "living-room"
#
# The token comes from SyncHub's Media > Players tab (Add player). Run it again at any time: it pulls the latest code and restarts the program.
#
# What it does, in order:
#   1. installs what is missing: git, python3, mpv, vainfo and the graphics driver tools
#   2. checks that SyncHub accepts the token (a wrong address or token stops here, before anything is changed)
#   3. creates the system user "synchubplayer" (groups video, render, input) and clones the code to /opt/synchub-player
#   4. writes /etc/synchub-player.ini (mode 600, it holds the token) and the systemd service, then starts it
# Exit status: 0 done, 1 failed.
set -Eeuo pipefail

REPO_URL_DEFAULT="https://github.com/ThebumGreg/letterboxd_scraper.git"
INI_FILE="${SYNCHUB_PLAYER_INI:-/etc/synchub-player.ini}"
SYSTEMD_DIR="${SYNCHUB_SYSTEMD_DIR:-/etc/systemd/system}"
STATE_DIR="${SYNCHUB_PLAYER_STATE:-/var/lib/synchub-player}"
PLAYER_USER="synchubplayer"

URL="${SYNCHUB_URL:-}"
TOKEN="${SYNCHUB_TOKEN:-}"
NAME=""
DIR="/opt/synchub-player"
REPO_URL="$REPO_URL_DEFAULT"
BRANCH=""
DO_START=1
DRY_RUN=0

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
run()  { if [ "$DRY_RUN" = 1 ]; then say "[dry run] $*"; else "$@"; fi; }

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; cat <<'USAGE'

Options:
  --url URL        SyncHub's address as this PC reaches it (not localhost)
  --token TOKEN    the player's token (sp_...)
  --name NAME      the player's name (default: this PC's host name)
  --dir DIR        where the code goes (default /opt/synchub-player)
  --repo URL       another repository (default: the SyncHub repository on GitHub)
  --branch NAME    a branch other than the repository's default
  --no-start       set everything up but do not start the service
  --dry-run        show what would be done and change nothing
A private repository needs a GitHub token with read access to Contents:  export GITHUB_TOKEN=...  and run  sudo -E ./player/setup-player.sh ...
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="${2:-}"; shift 2;;
        --token) TOKEN="${2:-}"; shift 2;;
        --name) NAME="${2:-}"; shift 2;;
        --dir) DIR="${2:-}"; shift 2;;
        --repo) REPO_URL="${2:-}"; shift 2;;
        --branch) BRANCH="${2:-}"; shift 2;;
        --no-start) DO_START=0; shift;;
        --dry-run) DRY_RUN=1; shift;;
        -h|--help) usage; exit 0;;
        *) die "Unknown option '$1'. Try --help.";;
    esac
done

URL="${URL%/}"
[ -n "$URL" ] || die "Give SyncHub's address with --url (for example http://192.168.1.20:5001)."
case "$URL" in http://*|https://*) ;; *) die "The address must start with http:// or https://.";; esac
[ -n "${SYNCHUB_ALLOW_LOCALHOST:-}" ] || case "$URL" in *localhost*|*127.0.0.1*) die "This PC cannot reach SyncHub at '$URL'. Use SyncHub's address on your network.";; esac
[ -n "$TOKEN" ] || die "Give the player's token with --token (Media > Players > Add player in SyncHub)."
[ -n "$NAME" ] || NAME="$(hostname)"
if [ "$DRY_RUN" != 1 ] && [ "$(id -u)" != 0 ]; then die "Run this with sudo."; fi

say "== 1. Packages"
if have apt-get; then
    missing=""
    for pkg in git python3 mpv vainfo curl; do dpkg -s "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"; done
    if [ -n "$missing" ]; then
        run apt-get update -qq
        # shellcheck disable=SC2086
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq intel-media-va-driver-non-free 2>/dev/null || warn "No Intel video driver package was installed (fine for AMD or NVIDIA graphics)."
    else
        say "Everything needed is installed."
    fi
else
    warn "apt-get was not found, so nothing was installed. This script expects Ubuntu or Debian."
fi

say "== 2. Does SyncHub accept the token?"
# Asking is harmless, so a dry run does it too. wait=0 so SyncHub answers at once instead of holding the request open.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"wait":0}' "$URL/api/player/v1/poll" || true)"
case "$code" in
    200) say "SyncHub answered: the token works.";;
    401) die "SyncHub refused the token. Copy it again from the Players tab (More > New token makes a fresh one).";;
    403) die "SyncHub says this player is disabled. Enable it in the Players tab first.";;
    000) die "Cannot reach SyncHub at $URL. Check the address and that this PC can reach it.";;
    *) die "SyncHub answered with HTTP $code at $URL/api/player/v1/poll. Is the address right and SyncHub up to date?";;
esac

say "== 3. User and code"
if ! id "$PLAYER_USER" >/dev/null 2>&1; then
    run useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin "$PLAYER_USER"
fi
for group in video render input; do
    getent group "$group" >/dev/null 2>&1 && run usermod -aG "$group" "$PLAYER_USER" || true
done
run mkdir -p "$STATE_DIR"
run chown "$PLAYER_USER": "$STATE_DIR"

if [ -n "${GITHUB_TOKEN:-}" ]; then
    export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
    GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
    export GIT_CONFIG_VALUE_0
fi
export GIT_TERMINAL_PROMPT=0
if [ -d "$DIR/.git" ]; then
    say "Updating $DIR"
    if [ "$DRY_RUN" = 1 ]; then
        say "[dry run] git fetch and fast-forward in $DIR"
    else
        git config --global --add safe.directory "$DIR" >/dev/null 2>&1 || true
        git -C "$DIR" fetch --quiet origin ${BRANCH:+"$BRANCH"} || die "git could not fetch from $REPO_URL. For a private repository set GITHUB_TOKEN (read access to Contents)."
        if [ -n "$BRANCH" ]; then git -C "$DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"; else git -C "$DIR" merge --quiet --ff-only '@{u}' || warn "$DIR has commits of its own, so it is used as it is."; fi
    fi
else
    [ ! -e "$DIR" ] || [ -z "$(ls -A "$DIR" 2>/dev/null)" ] || die "$DIR exists and is not a git checkout. Use --dir for another place."
    say "Cloning $REPO_URL into $DIR"
    run mkdir -p "$(dirname "$DIR")"
    if [ -n "$BRANCH" ]; then run git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR"; else run git clone --quiet "$REPO_URL" "$DIR"; fi
fi
run chown -R "$PLAYER_USER": "$DIR"
[ "$DRY_RUN" = 1 ] || [ -f "$DIR/player/synchub_player.py" ] || die "$DIR does not hold player/synchub_player.py. Is this the SyncHub repository, on a branch that has the player?"

say "== 4. Configuration and service"
if [ "$DRY_RUN" = 1 ]; then
    say "[dry run] write $INI_FILE (mode 600) and $SYSTEMD_DIR/synchub-player.service"
else
    umask 077
    {
        printf '[synchub]\nurl = %s\ntoken = %s\nname = %s\n' "$URL" "$TOKEN" "$NAME"
        [ -z "${GITHUB_TOKEN:-}" ] || printf 'github_token = %s\n' "$GITHUB_TOKEN"
    } > "$INI_FILE"
    chmod 600 "$INI_FILE"
    chown "$PLAYER_USER": "$INI_FILE"
    mkdir -p "$SYSTEMD_DIR"
    umask 022
    sed -e "s#^User=.*#User=$PLAYER_USER#" -e "s#/opt/synchub-player/player/synchub_player.py#$DIR/player/synchub_player.py#" -e "s#/etc/synchub-player.ini#$INI_FILE#" \
        "$DIR/player/synchub-player.service" > "$SYSTEMD_DIR/synchub-player.service"
fi
if have systemctl && [ "$SYSTEMD_DIR" = /etc/systemd/system ]; then
    run systemctl daemon-reload
    if [ "$DO_START" = 1 ]; then run systemctl enable synchub-player >/dev/null; run systemctl restart synchub-player; fi
fi

say "== Check"
if [ "$DRY_RUN" != 1 ]; then
    runuser -u "$PLAYER_USER" -- python3 "$DIR/player/synchub_player.py" --config "$INI_FILE" --check || warn "The check found a problem; read the lines above."
fi
say "Done. The player appears as Online on SyncHub's Media > Players tab within a few seconds."
