#!/usr/bin/env bash
# setup-worker.sh: sets up a SyncHub worker PC (Ubuntu or Debian) with one command.
#
#   git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper
#   sudo ./agent/setup-worker.sh --url http://192.168.1.20:5001 --token sw_...
#
# The token comes from SyncHub's Workers page (Add worker). Run it again at any time to update: it pulls the latest code from GitHub, asks
# SyncHub for the current share list and settings, rewrites only what changed and restarts the worker.
#
# What it does, in order:
#   1. installs what is missing (git, curl, Docker with Compose, cifs-utils / nfs-common; or python3 and ffmpeg with --mode native)
#   2. asks SyncHub for this worker's settings and shares (so the shares you defined on the Storage page are what gets mounted)
#   3. pulls the latest SyncHub code from GitHub (or clones it)
#   4. updates /etc/fstab: one clearly marked block with the shares, a backup of the old file, and a private credentials file for SMB
#   5. creates the mount folders and mounts the shares
#   6. writes the .env file (and docker-compose.yml), builds and starts the worker
#   7. writes the .synchub-root marker into each mounted share (once) and checks that everything works
#
# It never touches /etc/fstab lines outside its own block, never overwrites a share's files and never writes into an unmounted folder.
# Exit status: 0 done, 1 failed, 3 done but something needs your attention (the summary says what).
set -Eeuo pipefail

REPO_URL_DEFAULT="https://github.com/ThebumGreg/letterboxd_scraper.git"
SELF="${BASH_SOURCE[0]:-}"
SELF_DIR=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then SELF_DIR="$(cd "$(dirname "$SELF")" && pwd)"; fi

# Everything a person may want to change is a flag below. These paths exist as variables so tests can point them elsewhere.
FSTAB="${SYNCHUB_FSTAB:-/etc/fstab}"
CRED_FILE="${SYNCHUB_CRED_FILE:-/etc/synchub-smb.cred}"
INI_FILE="${SYNCHUB_INI_FILE:-/etc/synchub-agent.ini}"
SYSTEMD_DIR="${SYNCHUB_SYSTEMD_DIR:-/etc/systemd/system}"
AGENT_DIR="${SYNCHUB_AGENT_DIR:-/opt/synchub-agent}"

BEGIN_MARK="# BEGIN synchub-worker (written by setup-worker.sh; it replaces this block each time it runs, so edit outside it)"
END_MARK="# END synchub-worker"

URL="${SYNCHUB_URL:-}"
TOKEN="${SYNCHUB_TOKEN:-}"
MODE="docker"
DIR="/opt/synchub-worker"
WORK_DIR=""
REPO_URL=""
REPO_DIR=""
BRANCH=""
SMB_USER_VALUE="${SMB_USER:-}"
SMB_PASSWORD_VALUE="${SMB_PASSWORD:-}"
DO_MOUNTS=1
DO_INIT_ROOTS=1
DO_START=1
DO_GPU=1
DO_WATCHDOG=1
DO_AUTO_UPDATE=1
ADOPT=0
GPU_FOUND=""
DRI_DIR="${SYNCHUB_DRI_DIR:-/dev/dri}"
DRY_RUN=0
ASSUME_YES=0
WARNINGS=0
TMP=""
APT_UPDATED=0

# ---------------------------------------------------------------------------------------------------------------- output
if [ -t 1 ]; then BOLD=$'\033[1m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; GREEN=$'\033[32m'; OFF=$'\033[0m'; else BOLD=""; RED=""; YELLOW=""; GREEN=""; OFF=""; fi
STEP=0
step() { STEP=$((STEP + 1)); printf '\n%s[%s] %s%s\n' "$BOLD" "$STEP" "$*" "$OFF"; }
say() { printf '    %s\n' "$*"; }
warn() { WARNINGS=$((WARNINGS + 1)); printf '    %sWARNING:%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die() { printf '\n%sERROR:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
on_error() { printf '\n%sERROR:%s setup-worker.sh stopped at line %s (exit %s). Nothing after that point was done; fix the cause and run it again.\n' "$RED" "$OFF" "$2" "$1" >&2; }
trap 'on_error $? $LINENO' ERR
cleanup() { if [ -n "$TMP" ] && [ -d "$TMP" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT

run() {
    if [ "$DRY_RUN" = 1 ]; then printf '    [dry run] %s\n' "$*"; else "$@"; fi
}

usage() {
    cat <<'EOF'
Usage: sudo ./agent/setup-worker.sh --url http://SYNCHUB:5001 --token sw_... [options]

Sets up (or updates) a SyncHub worker on this Ubuntu or Debian PC: installs the dependencies, pulls the latest code from GitHub, mounts the
shares through /etc/fstab, creates the .env file and starts the worker. Running it again updates everything.

Required the first time (afterwards they are read from the earlier run):
  --url URL             SyncHub's address as this PC reaches it, for example http://192.168.1.20:5001 (not localhost)
  --token TOKEN         the worker's token from SyncHub's Workers page. Also read from $SYNCHUB_TOKEN, or asked for.

Options:
  --mode docker|native  docker (default): the worker runs in a container. native: python3 + ffmpeg and a systemd service, no Docker.
  --dir DIR             where the worker's files live (default /opt/synchub-worker): .env, docker-compose.yml and the work folder
  --work-dir DIR        job folders, kept originals and logs; use a big local disk (default DIR/work, or /var/lib/synchub-agent for native)
  --repo URL            where to get the code (default: the checkout this script is in, else the GitHub repository)
  --repo-dir DIR        where the code is (default: the checkout this script is in, else DIR/repo)
  --branch NAME         the branch to run (default: the one checked out, or the repository's default for a new clone)
  --smb-user USER       the Unraid user that may read and write the shares (SMB). Also $SMB_USER, or asked for.
  --smb-password PASS   its password. Prefer $SMB_PASSWORD or the prompt: a command line is visible to other users of this PC.
  --no-mounts           do not touch /etc/fstab and do not mount anything (the shares are already mounted where SyncHub says)
  --no-init-roots       do not write the .synchub-root marker files into the shares
  --no-start            set everything up but do not build and start the worker
  --no-gpu              do not give the container a GPU even if one is found (/dev/dri for Intel and AMD, nvidia-smi for NVIDIA)
  --no-watchdog         do not install the mount watchdog (agent/mount-watchdog.sh) that remounts the shares after a network outage
  --no-auto-update      do not install the update timer (agent/auto-update.sh) behind the Workers page's Update button
  --adopt               take over a worker that was set up by hand: keep its /etc/fstab lines and mounts, back up its .env, compose file and fstab first
                        (same as --no-mounts --no-init-roots, plus the backup)
  --dry-run             show what would be done; changes nothing (it still asks SyncHub for the settings, which proves the address and token)
  -y, --yes             never ask questions; fail instead
  -h, --help            this text

A private repository needs a GitHub token with read access to Contents:  export GITHUB_TOKEN=...  and run  sudo -E ./agent/setup-worker.sh ...
(-E keeps the variable through sudo). It is used for this run only and is never written to disk.
EOF
}

# ---------------------------------------------------------------------------------------------------------------- arguments
parse_args() {
    while [ $# -gt 0 ]; do
        local opt="$1" value=""
        case "$opt" in
            --*=*) value="${opt#*=}"; opt="${opt%%=*}" ;;
        esac
        case "$opt" in
            -h|--help) usage; exit 0 ;;
            -y|--yes) ASSUME_YES=1 ;;
            --dry-run) DRY_RUN=1 ;;
            --no-mounts) DO_MOUNTS=0 ;;
            --no-init-roots) DO_INIT_ROOTS=0 ;;
            --no-start) DO_START=0 ;;
            --no-gpu) DO_GPU=0 ;;
            --no-watchdog) DO_WATCHDOG=0 ;;
            --no-auto-update) DO_AUTO_UPDATE=0 ;;
            --adopt) ADOPT=1; DO_MOUNTS=0; DO_INIT_ROOTS=0 ;;
            --url|--token|--mode|--dir|--work-dir|--repo|--repo-dir|--branch|--smb-user|--smb-password)
                if [[ "$1" != *=* ]]; then
                    [ $# -ge 2 ] || die "$opt needs a value."
                    value="$2"; shift
                fi
                case "$opt" in
                    --url) URL="$value" ;;
                    --token) TOKEN="$value" ;;
                    --mode) MODE="$value" ;;
                    --dir) DIR="$value" ;;
                    --work-dir) WORK_DIR="$value" ;;
                    --repo) REPO_URL="$value" ;;
                    --repo-dir) REPO_DIR="$value" ;;
                    --branch) BRANCH="$value" ;;
                    --smb-user) SMB_USER_VALUE="$value" ;;
                    --smb-password) SMB_PASSWORD_VALUE="$value" ;;
                esac
                ;;
            *) usage >&2; die "Unknown option '$1'." ;;
        esac
        shift
    done
    case "$MODE" in docker|native) ;; *) die "--mode must be docker or native." ;; esac
}

can_prompt() { [ "$ASSUME_YES" != 1 ] && { true </dev/tty; } 2>/dev/null; }

ask() { # ask VARIABLE "question" [secret]
    local var="$1" question="$2" secret="${3:-}" reply=""
    can_prompt || return 1
    if [ -n "$secret" ]; then read -r -s -p "    $question: " reply </dev/tty; echo >/dev/tty; else read -r -p "    $question: " reply </dev/tty; fi
    [ -n "$reply" ] || return 1
    printf -v "$var" '%s' "$reply"
}

# ---------------------------------------------------------------------------------------------------------------- helpers
env_value() { # env_value KEY FILE: the last KEY=value in a .env file (read, never sourced)
    [ -f "$2" ] || return 0
    sed -n "s/^$1=//p" "$2" | tail -n 1
}

ini_value() { # ini_value KEY FILE
    [ -f "$2" ] || return 0
    sed -n "s/^$1[[:space:]]*=[[:space:]]*//p" "$2" | head -n 1
}

is_root() { [ "$(id -u)" = 0 ]; }

have() { command -v "$1" >/dev/null 2>&1; }

apt_install() {
    have apt-get || die "This PC has no apt-get. Install these yourself, then run the script again: $*"
    if [ "$APT_UPDATED" = 0 ]; then run env DEBIAN_FRONTEND=noninteractive apt-get update -qq; APT_UPDATED=1; fi
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@"
}

ensure_commands() { # ensure_commands "command:package" ...: installs the package for each command that is missing
    local missing=() spec
    for spec in "$@"; do
        have "${spec%%:*}" || missing+=("${spec##*:}")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        say "Installing: ${missing[*]}"
        apt_install "${missing[@]}"
    fi
}

curl_setup() { # curl_setup NAME [QUERY]: one of this worker's setup files from SyncHub, as text on stdout
    local name="$1" query="${2:-}" out status
    have curl || die "curl is needed to ask SyncHub for the settings (sudo apt install curl)."
    out="$TMP/response"
    status="$(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -sS -o "$out" -w '%{http_code}' -X POST --noproxy '*' --connect-timeout 10 --max-time 60 \
        -K - "$URL/api/worker/v1/setup/$name$query")" || die "Cannot reach SyncHub at $URL. Check the address (not localhost), that SyncHub is running and that this PC can reach its port."
    case "$status" in
        200) cat "$out" ;;
        401) die "SyncHub does not accept this token. Was it replaced or revoked? Copy the current token from the Workers page (More, New token)." ;;
        403) die "This worker is disabled in SyncHub (Workers page, More, Enable)." ;;
        404) die "SyncHub at $URL does not have the setup endpoint. Update SyncHub to this version first (it is part of the same release as this script)." ;;
        *) die "SyncHub answered HTTP $status: $(head -c 300 "$out")" ;;
    esac
}

# ---------------------------------------------------------------------------------------------------------------- the steps
become_root() {
    if is_root || [ "$DRY_RUN" = 1 ] || [ "${SYNCHUB_SETUP_ALLOW_NONROOT:-}" = 1 ]; then return 0; fi
    if [ -n "$SELF_DIR" ] && have sudo; then
        say "Needs root; running again through sudo."
        exec sudo -E bash "$SELF" "$@"
    fi
    die "Run this as root: sudo ./agent/setup-worker.sh ..."
}

load_previous_settings() {
    local env_file="$DIR/.env"
    [ -n "$URL" ] || URL="$(env_value SYNCHUB_URL "$env_file")"
    [ -n "$TOKEN" ] || TOKEN="$(env_value SYNCHUB_TOKEN "$env_file")"
    [ -n "$WORK_DIR" ] || WORK_DIR="$(env_value WORK_DIR "$env_file")"
    if [ "$MODE" = native ]; then
        [ -n "$URL" ] || URL="$(ini_value url "$INI_FILE")"
        [ -n "$TOKEN" ] || TOKEN="$(ini_value token "$INI_FILE")"
        [ -n "$WORK_DIR" ] || WORK_DIR="$(ini_value work_dir "$INI_FILE")"
    fi
    [ -n "$URL" ] || ask URL "SyncHub's address (for example http://192.168.1.20:5001)" || true
    [ -n "$TOKEN" ] || ask TOKEN "This worker's token (from SyncHub's Workers page)" secret || true
    [ -n "$URL" ] || die "SyncHub's address is missing. Use --url http://192.168.1.20:5001"
    [ -n "$TOKEN" ] || die "The worker's token is missing. Use --token sw_... (add the worker on SyncHub's Workers page to get one)."
    URL="${URL%/}"
    [[ "$URL" =~ ^https?://[^/[:space:]]+ ]] || die "'$URL' is not an address. It looks like http://192.168.1.20:5001"
    case "${SYNCHUB_SETUP_ALLOW_LOCALHOST:+allowed}$URL" in
        http://localhost*|http://127.*|https://localhost*|https://127.*|http://\[::1\]*) die "'$URL' means 'this PC'. Use SyncHub's address on your network, such as http://192.168.1.20:5001" ;;
    esac
    [ -n "$WORK_DIR" ] || { if [ "$MODE" = native ]; then WORK_DIR=/var/lib/synchub-agent; else WORK_DIR="$DIR/work"; fi; }
    case "$WORK_DIR" in ./*) WORK_DIR="$DIR/${WORK_DIR#./}" ;; esac          # SyncHub's own sample .env says ./work, meaning next to the .env
    for value in "$DIR" "$WORK_DIR"; do
        [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "'$value' is not usable: use an absolute path of letters, numbers and . _ / - only."
    done
}

choose_code_location() {
    local own_checkout=""
    if [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/../.git" ] && [ -f "$SELF_DIR/synchub_agent.py" ]; then own_checkout="$(cd "$SELF_DIR/.." && pwd)"; fi
    [ -n "$REPO_DIR" ] || REPO_DIR="${own_checkout:-$DIR/repo}"
    if [ -z "$REPO_URL" ]; then
        if [ -d "$REPO_DIR/.git" ]; then REPO_URL="$(git_repo config --get remote.origin.url 2>/dev/null || true)"; fi
        [ -n "$REPO_URL" ] || REPO_URL="$REPO_URL_DEFAULT"
    fi
    case "$REPO_DIR" in
        /tmp/*|/var/tmp/*|/dev/shm/*|/run/*) [ -n "${SYNCHUB_SETUP_ALLOW_TEMP_CHECKOUT:-}" ] || die "'$REPO_DIR' is a temporary folder. The worker is built from this checkout and its updates are pulled into it, so clone it somewhere permanent, for example: git clone <repository> ~/letterboxd_scraper" ;;
    esac
    [[ "$REPO_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "'$REPO_DIR' is not usable: use an absolute path of letters, numbers and . _ / - only."
}

# git inside the code folder, as the folder's owner (root would leave root-owned files in someone's checkout, and git refuses a folder it does not own)
git_repo() {
    local owner="" home=""
    if [ -d "$REPO_DIR" ] && is_root; then owner="$(stat -c %U "$REPO_DIR" 2>/dev/null || true)"; fi
    if [ -n "$owner" ] && [ "$owner" != root ] && have runuser; then
        home="$(getent passwd "$owner" | cut -d: -f6)"
        runuser -u "$owner" -- env HOME="${home:-/tmp}" git -C "$REPO_DIR" "$@"
    else
        git -C "$REPO_DIR" "$@"
    fi
}

git_auth() { # a private repository: the token travels in the environment of this run only, never in a URL, a config file or a process list
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
        GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
        export GIT_CONFIG_VALUE_0
    fi
    export GIT_TERMINAL_PROMPT=0
}

sync_code() {
    git_auth
    if [ -d "$REPO_DIR/.git" ]; then
        say "Updating $REPO_DIR from $REPO_URL"
        if [ -n "$(git_repo status --porcelain --untracked-files=no)" ]; then
            warn "$REPO_DIR has local changes, so it is used as it is and not updated. Commit or discard them to get the latest code."
        elif [ "$DRY_RUN" = 1 ]; then
            say "[dry run] git fetch, then fast-forward ${BRANCH:+to $BRANCH}"
        else
            local current
            current="$(git_repo rev-parse --abbrev-ref HEAD)"
            [ -n "$BRANCH" ] || BRANCH="$current"
            [ "$BRANCH" != HEAD ] || die "$REPO_DIR is not on a branch. Check one out, or pass --branch."
            git_repo fetch --quiet origin "$BRANCH" || die "git could not fetch '$BRANCH' from GitHub. For a private repository set GITHUB_TOKEN (read access to Contents)."
            if [ "$BRANCH" != "$current" ]; then git_repo checkout --quiet -B "$BRANCH" "origin/$BRANCH"; else git_repo merge --quiet --ff-only FETCH_HEAD \
                || warn "$REPO_DIR has commits of its own on '$BRANCH', so it cannot be fast-forwarded. It is used as it is."; fi
        fi
    else
        [ ! -e "$REPO_DIR" ] || [ -z "$(ls -A "$REPO_DIR" 2>/dev/null)" ] || die "$REPO_DIR exists and is not a git checkout. Use --repo-dir for another place."
        say "Cloning $REPO_URL into $REPO_DIR"
        run mkdir -p "$(dirname "$REPO_DIR")"
        if [ -n "$BRANCH" ]; then run git clone --quiet --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"; else run git clone --quiet "$REPO_URL" "$REPO_DIR"; fi
    fi
    if [ "$DRY_RUN" != 1 ]; then
        [ -f "$REPO_DIR/agent/synchub_agent.py" ] || die "$REPO_DIR does not hold the SyncHub agent (agent/synchub_agent.py). Is that the right repository and branch?"
        say "Code: $(git_repo log -1 --format='%h %s (%cr)')"
    fi
}

fetch_settings() {
    local query=""
    [ "$MODE" = docker ] && query="?mounts=host"
    curl_setup ".env" "$query" >"$TMP/env"
    curl_setup docker-compose.yml "$query" >"$TMP/compose"
    curl_setup synchub-agent.ini >"$TMP/ini"
    curl_setup fstab >"$TMP/fstab.all"
    grep -vE '^[[:space:]]*(#|$)' "$TMP/fstab.all" >"$TMP/fstab.entries" || true
    SHARES="$(wc -l <"$TMP/fstab.entries" | tr -d ' ')"
    HAS_SMB=0; HAS_NFS=0
    if awk '$3 == "cifs" {found = 1} END {exit !found}' "$TMP/fstab.entries"; then HAS_SMB=1; fi
    if awk '$3 == "nfs" {found = 1} END {exit !found}' "$TMP/fstab.entries"; then HAS_NFS=1; fi
    say "SyncHub accepted the token. Shares to mount: $SHARES (SMB: $HAS_SMB, NFS: $HAS_NFS)"
    if [ "$SHARES" = 0 ]; then warn "SyncHub has no mountable shares yet. Add them on its Storage page and run this script again."; fi
}

install_dependencies() {
    ensure_commands git:git curl:curl update-ca-certificates:ca-certificates
    if [ "$DO_MOUNTS" = 1 ]; then
        if [ "$HAS_SMB" = 1 ]; then ensure_commands mount.cifs:cifs-utils; fi
        if [ "$HAS_NFS" = 1 ]; then ensure_commands mount.nfs:nfs-common; fi
    fi
    if [ "$MODE" = docker ]; then
        ensure_docker
    else
        ensure_commands python3:python3 ffmpeg:ffmpeg ffprobe:ffmpeg
        have systemctl || die "--mode native needs systemd (systemctl). Use the Docker mode instead."
        ensure_agent_user
    fi
}

ensure_docker() {
    if ! have docker; then
        say "Installing Docker (docker.io from the distribution)"
        apt_install docker.io
    fi
    if [ "$DRY_RUN" != 1 ] && ! docker compose version >/dev/null 2>&1; then
        say "Installing Docker Compose"
        apt_install docker-compose-v2 || apt_install docker-compose-plugin \
            || die "Could not install Docker Compose. Install the 'docker compose' plugin (https://docs.docker.com/compose/install/linux/) and run this again."
    fi
    if have systemctl; then run systemctl enable --now docker; fi
    if [ "$DRY_RUN" != 1 ]; then docker info >/dev/null 2>&1 || die "Docker is installed but its service does not answer ('docker info' fails). Start it (sudo systemctl start docker) and run this again."; fi
}

write_credentials() {
    local stored_login
    if [ "$DO_MOUNTS" != 1 ] || [ "$HAS_SMB" != 1 ]; then return 0; fi
    if [ -z "$SMB_USER_VALUE$SMB_PASSWORD_VALUE" ] && stored_login="$(curl_setup smb.cred 2>/dev/null)"; then
        # The login saved on SyncHub's Storage page: no question to answer.
        if [ "$DRY_RUN" = 1 ]; then say "[dry run] would write $CRED_FILE from the login stored in SyncHub"; return 0; fi
        ( umask 077; printf '%s\n' "$stored_login" >"$CRED_FILE.new" )
        chmod 600 "$CRED_FILE.new"
        mv -f "$CRED_FILE.new" "$CRED_FILE"
        say "Wrote $CRED_FILE (readable by root only) from the share login stored in SyncHub"
        return 0
    fi
    if [ -z "$SMB_USER_VALUE" ] && [ -f "$CRED_FILE" ]; then
        say "Keeping the SMB login in $CRED_FILE (pass --smb-user and --smb-password to change it)"
        return 0
    fi
    [ -n "$SMB_USER_VALUE" ] || ask SMB_USER_VALUE "The Unraid user that may read and write the shares" || true
    [ -n "$SMB_PASSWORD_VALUE" ] || ask SMB_PASSWORD_VALUE "Its password" secret || true
    if [ -z "$SMB_USER_VALUE" ] || [ -z "$SMB_PASSWORD_VALUE" ]; then
        die "The shares use SMB, which needs the Unraid user and password. Use --smb-user and --smb-password (or SMB_USER and SMB_PASSWORD)."
    fi
    case "$SMB_USER_VALUE$SMB_PASSWORD_VALUE" in *$'\n'*) die "The user name and password cannot contain a line break." ;; esac
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] would write $CRED_FILE (private, mode 600) with the SMB login"; return 0; fi
    ( umask 077; printf 'username=%s\npassword=%s\n' "$SMB_USER_VALUE" "$SMB_PASSWORD_VALUE" >"$CRED_FILE.new" )
    chmod 600 "$CRED_FILE.new"
    mv -f "$CRED_FILE.new" "$CRED_FILE"
    say "Wrote $CRED_FILE (readable by root only)"
}

# The fstab entries to add, with the agent's user in native mode (a CIFS mount is owned by whoever mounts it, so a service that does not run as root needs uid/gid).
fstab_entries() {
    if [ "$MODE" = native ]; then
        local uid gid
        uid="$(id -u synchub 2>/dev/null || echo 0)"; gid="$(id -g synchub 2>/dev/null || echo 0)"
        awk -v uid="$uid" -v gid="$gid" '$3 == "cifs" && $4 !~ /(^|,)uid=/ && uid != 0 {$4 = $4 ",uid=" uid ",gid=" gid ",file_mode=0664,dir_mode=0775"} {print}' "$TMP/fstab.entries"
    else
        cat "$TMP/fstab.entries"
    fi
}

update_fstab() {
    [ "$DO_MOUNTS" = 1 ] || { say "Skipping /etc/fstab and the mounts (--no-mounts)."; return 0; }
    local old="$TMP/fstab.old" kept="$TMP/fstab.kept" fresh="$TMP/fstab.new" entries="$TMP/fstab.mine" taken line mp
    if [ -f "$FSTAB" ]; then cp "$FSTAB" "$old"; else : >"$old"; fi
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$old" >"$kept"
    taken="$(awk '!/^[[:space:]]*#/ && NF >= 2 {print $2}' "$kept")"
    : >"$entries"
    fstab_entries >"$TMP/fstab.wanted"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        mp="$(awk '{print $2}' <<<"$line")"
        if grep -qxF -- "$mp" <<<"$taken"; then
            warn "/etc/fstab already has its own line for $mp, so SyncHub's line for it was not added. Check that yours is the right share."
            continue
        fi
        printf '%s\n' "$line" >>"$entries"
    done <"$TMP/fstab.wanted"
    cp "$kept" "$fresh"
    if [ -s "$entries" ]; then
        # one empty line before the block, but never more than one after repeated runs
        if [ -s "$fresh" ] && [ -n "$(tail -n 1 "$fresh")" ]; then echo >>"$fresh"; fi
        { echo "$BEGIN_MARK"; cat "$entries"; echo "$END_MARK"; } >>"$fresh"
    fi
    if cmp -s "$old" "$fresh"; then
        say "$FSTAB is already up to date"
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        say "[dry run] would change $FSTAB (a backup is kept). The SyncHub block would be:"
        { echo "$BEGIN_MARK"; cat "$entries"; echo "$END_MARK"; } | sed 's/^/        /'
        return 0
    fi
    if [ -f "$FSTAB" ]; then
        local backup
        backup="$FSTAB.synchub-backup-$(date +%Y%m%d-%H%M%S)"
        cp -p "$FSTAB" "$backup"
        say "Backed up $FSTAB to $backup"
    fi
    # written beside the real file and moved over it, so a power cut cannot leave half an fstab
    if [ -f "$FSTAB" ]; then cp -p "$FSTAB" "$FSTAB.synchub-new"; else : >"$FSTAB.synchub-new"; chmod 644 "$FSTAB.synchub-new"; fi
    cat "$fresh" >"$FSTAB.synchub-new"
    mv -f "$FSTAB.synchub-new" "$FSTAB"
    say "Updated $FSTAB: $(wc -l <"$entries" | tr -d ' ') share line(s) in the SyncHub block"
    if have systemctl && is_root; then systemctl daemon-reload || true; fi
}

mount_points() { awk '{print $2}' "$TMP/fstab.entries"; }

mount_shares() {
    [ "$DO_MOUNTS" = 1 ] || return 0
    local mp output
    UNMOUNTED=0
    while IFS= read -r mp; do
        [ -n "$mp" ] || continue
        if [ "$DRY_RUN" = 1 ]; then say "[dry run] mkdir -p $mp; mount $mp"; continue; fi
        mkdir -p "$mp"
        if mountpoint -q "$mp"; then say "$mp is already mounted"; continue; fi
        if output="$(mount "$mp" 2>&1)"; then
            say "Mounted $mp"
        else
            UNMOUNTED=$((UNMOUNTED + 1))
            warn "Could not mount $mp: $output
        Check the address, share name, user and password. For SMB try vers=3.1.1 or vers=2.1 in the share's options on SyncHub's Storage page. Then run this script again."
        fi
    done < <(mount_points)
}

all_mounted() {
    local mp
    while IFS= read -r mp; do
        [ -n "$mp" ] || continue
        mountpoint -q "$mp" || return 1
    done < <(mount_points)
}

# Writes TARGET from SOURCE only when the text differs (keeping one .previous copy), so a re-run does not restart anything for nothing.
install_file() { # install_file SOURCE TARGET MODE [OWNER:GROUP]
    local source="$1" target="$2" mode="$3" owner="${4:-}"
    if [ -f "$target" ] && cmp -s "$source" "$target"; then return 0; fi
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] would write $target"; return 0; fi
    mkdir -p "$(dirname "$target")"
    if [ -f "$target" ]; then cp -p "$target" "$target.previous"; fi
    install -m "$mode" "$source" "$target"
    if [ -n "$owner" ]; then chown "$owner" "$target"; fi
    say "Wrote $target"
}

# --adopt: a copy of what the hand-made setup had, in a folder next to it, before anything is replaced.
adopt_backup() {
    local stamp backup file
    stamp="$(date +%Y%m%d-%H%M%S)"; backup="$DIR/adopt-backup-$stamp"
    mkdir -p "$backup"
    for file in "$DIR/.env" "$DIR/docker-compose.yml" "$DIR/docker-compose.yaml" "$FSTAB"; do
        if [ -f "$file" ]; then cp -p "$file" "$backup/$(basename "$file")"; fi
    done
    chmod 700 "$backup"
    say "Adopting this worker: its .env, compose file and fstab were copied to $backup. The fstab lines and mounts are left as they are."
    if [ -f "$DIR/docker-compose.yml" ] && ! cmp -s "$TMP/compose.final" "$DIR/docker-compose.yml"; then
        say "The compose file will change (diff below, - is the old one):"
        diff -u "$DIR/docker-compose.yml" "$TMP/compose.final" | sed 's/^/        /' | head -n 60 || true
    fi
}

write_docker_files() {
    sed -E "s|^(WORK_DIR=).*|\\1$WORK_DIR|" "$TMP/env" >"$TMP/env.final"
    grep -q "^WORK_DIR=$WORK_DIR\$" "$TMP/env.final" || die "SyncHub's .env has no WORK_DIR line; is SyncHub the same release as this script?"
    sed -E "s|^(    build: )\./agent\$|\\1\"$REPO_DIR/agent\"|" "$TMP/compose" >"$TMP/compose.final"
    grep -q "build: \"$REPO_DIR/agent\"" "$TMP/compose.final" || die "SyncHub's docker-compose.yml has no 'build: ./agent' line; is SyncHub the same release as this script?"
    add_gpu_to_compose
    run mkdir -p "$DIR" "$WORK_DIR"
    if [ "$ADOPT" = 1 ] && [ "$DRY_RUN" != 1 ]; then adopt_backup; fi
    install_file "$TMP/env.final" "$DIR/.env" 600
    install_file "$TMP/compose.final" "$DIR/docker-compose.yml" 644
    # Mounts that were made after Docker started (boot order, a remount) only show inside the container if the host's mounts are shared.
    if [ "$DRY_RUN" != 1 ] && have findmnt && [ "$DO_MOUNTS" = 1 ] && [ "$(findmnt -no PROPAGATION / 2>/dev/null)" = private ]; then
        mount --make-rshared / || warn "Could not make the root mount shared; Docker may refuse the share folders ('not a shared or slave mount')."
    fi
}

# Which GPU this PC has, if any: "dri" (Intel or AMD, /dev/dri/renderD*), "nvidia" (nvidia-smi answers) or "". The agent still test-encodes a frame with every
# encoder before it offers one, so a wrong guess here costs nothing; it only decides what the container is allowed to see.
detect_gpu() {
    if [ "$DO_GPU" != 1 ]; then return 0; fi
    if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then echo nvidia; return 0; fi
    if compgen -G "$DRI_DIR/renderD*" >/dev/null; then echo dri; fi
}

# NVIDIA GPUs reach a container through the NVIDIA Container Toolkit: install it and register it with Docker when it is not there yet.
ensure_nvidia_runtime() {
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] would make sure the NVIDIA Container Toolkit is installed"; return 0; fi
    if docker info 2>/dev/null | grep -qi 'runtimes:.*nvidia'; then return 0; fi
    say "Installing the NVIDIA Container Toolkit so the container can use the NVIDIA GPU"
    if ! { apt_install nvidia-container-toolkit && nvidia-ctk runtime configure --runtime=docker && systemctl restart docker; }; then
        warn "Could not set up the NVIDIA Container Toolkit (it is not in this PC's package sources: see https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html). The worker will encode on the CPU."
        return 1
    fi
}

# Adds the GPU to the generated compose file (in $TMP/compose.final), so there is one file and one command, never a second -f file.
add_gpu_to_compose() {
    local kind
    kind="$(detect_gpu)"
    if [ -z "$kind" ]; then say "No GPU found (or --no-gpu): the worker will encode on the CPU."; return 0; fi
    GPU_FOUND="$kind"
    case "$kind" in
        dri)    say "Found a GPU at $DRI_DIR (Intel or AMD): giving the container access to it"
                sed -i -E 's|^(    init: true)$|\1\n    devices:\n      - /dev/dri:/dev/dri|' "$TMP/compose.final" ;;
        nvidia) say "Found an NVIDIA GPU: giving the container access to it"
                if ensure_nvidia_runtime; then sed -i -E 's|^(    init: true)$|\1\n    gpus: all|' "$TMP/compose.final"; fi ;;
    esac
    grep -qE '^    (devices|gpus):' "$TMP/compose.final" || [ "$kind" = nvidia ] || die "SyncHub's docker-compose.yml has no 'init: true' line to add the GPU after; is SyncHub the same release as this script?"
}

ensure_agent_user() {
    if ! id synchub >/dev/null 2>&1; then
        run useradd --system --home-dir /var/lib/synchub-agent --shell /usr/sbin/nologin synchub
        [ "$DRY_RUN" = 1 ] || say "Created the system user 'synchub'"
    fi
    local group
    for group in video render; do
        if getent group "$group" >/dev/null 2>&1; then run usermod -aG "$group" synchub; fi     # lets the agent use a GPU for encoding
    done
}

write_native_files() {
    sed -E "s|^(work_dir = ).*|\\1$WORK_DIR|" "$TMP/ini" >"$TMP/ini.final"
    run install -d -m 755 "$AGENT_DIR"
    run install -d -m 755 -o synchub -g synchub "$WORK_DIR"
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] would copy the agent to $AGENT_DIR, write $INI_FILE and the systemd service"; return 0; fi
    local file
    for file in synchub_agent.py media_analysis.py; do install_file "$REPO_DIR/agent/$file" "$AGENT_DIR/$file" 644; done
    install_file "$TMP/ini.final" "$INI_FILE" 640 root:synchub
    install_file "$REPO_DIR/agent/synchub-agent.service" "$SYSTEMD_DIR/synchub-agent.service" 644
    if have systemctl; then systemctl daemon-reload; fi
}

start_docker() {
    if [ "$DO_START" != 1 ]; then say "Not starting the worker (--no-start). Start it with:  cd $DIR && docker compose up -d --build"; return 0; fi
    say "Building the worker image and starting it (the first build takes a minute or two)"
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] cd $DIR && docker compose up -d --build"; return 0; fi
    ( cd "$DIR" && docker compose up -d --build ) || die "Docker could not start the worker (see above). 'cd $DIR && docker compose logs' may say more."
    say "The worker container is up"
}

start_native() {
    if [ "$DO_START" != 1 ]; then say "Not starting the worker (--no-start). Start it with:  sudo systemctl enable --now synchub-agent"; return 0; fi
    run systemctl enable synchub-agent
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] systemctl restart synchub-agent"; return 0; fi
    systemctl restart synchub-agent
    say "The synchub-agent service is running"
}

# The mount watchdog and the update timer: separate scripts, installed here so a new worker needs one command and no second visit.
install_extras() {
    if [ "$MODE" != docker ]; then return 0; fi
    if ! have systemctl; then say "No systemd here: skipping the mount watchdog and the update timer."; return 0; fi
    if [ "$DO_WATCHDOG" = 1 ] && [ "$SHARES" -gt 0 ]; then
        say "Installing the mount watchdog (remounts the shares after a network outage)"
        # shellcheck disable=SC2086
        if [ "$DRY_RUN" = 1 ]; then say "[dry run] $REPO_DIR/agent/mount-watchdog.sh --install"
        else SYNCHUB_MOUNT_PATHS="$(mount_points | tr '\n' ' ')" bash "$REPO_DIR/agent/mount-watchdog.sh" --install ${SYNCHUB_EXTRAS_FLAGS:-} || warn "Could not install the mount watchdog; run  sudo $REPO_DIR/agent/mount-watchdog.sh --install  yourself."; fi
    fi
    if [ "$DO_AUTO_UPDATE" = 1 ]; then
        say "Installing the update timer (the Workers page's Update button and automatic updates)"
        # shellcheck disable=SC2086
        if [ "$DRY_RUN" = 1 ]; then say "[dry run] $REPO_DIR/agent/auto-update.sh --install --dir $DIR"
        else SYNCHUB_REPO="$REPO_DIR" bash "$REPO_DIR/agent/auto-update.sh" --install --dir "$DIR" ${SYNCHUB_EXTRAS_FLAGS:-} || warn "Could not install the update timer; run  sudo $REPO_DIR/agent/auto-update.sh --install --dir $DIR  yourself."; fi
    fi
}

agent_command() { # agent_command ARGS...: the agent's own command line tools (--init-roots, --check) in the way the worker runs
    if [ "$MODE" = docker ]; then
        ( cd "$DIR" && docker compose run --rm worker "$@" )
    else
        if is_root; then runuser -u synchub -- python3 "$AGENT_DIR/synchub_agent.py" --config "$INI_FILE" "$@"; else python3 "$AGENT_DIR/synchub_agent.py" --config "$INI_FILE" "$@"; fi
    fi
}

verify() {
    if [ "$DO_START" != 1 ]; then return 0; fi
    if [ "$DRY_RUN" = 1 ]; then say "[dry run] would write the marker files and run the agent's --check"; return 0; fi
    if [ "$DO_INIT_ROOTS" = 1 ] && [ "$SHARES" -gt 0 ]; then
        if [ "$DO_MOUNTS" = 1 ] && ! all_mounted; then
            warn "Not every share is mounted, so the marker files were not written (that would put them in empty local folders). Fix the mounts and run this script again."
        else
            say "Marking the shares (writes .synchub-root into each, once; it never creates a folder)"
            agent_command --init-roots || warn "Not every share could be marked; the lines above say which and why."
        fi
    fi
    if ! agent_command --check; then warn "The check found problems; the lines above say what to fix. Run this script again afterwards."; fi
}

summary() {
    printf '\n%s================================================================%s\n' "$BOLD" "$OFF"
    if [ "$DRY_RUN" = 1 ]; then
        printf '%sDry run finished.%s Nothing was changed. Run again without --dry-run to set the worker up.\n' "$BOLD" "$OFF"
    elif [ "$WARNINGS" -gt 0 ]; then
        printf '%sDone, with %s thing(s) that need your attention%s (marked WARNING above).\n' "$YELLOW" "$WARNINGS" "$OFF"
    else
        printf '%sDone.%s\n' "$GREEN" "$OFF"
    fi
    echo "  SyncHub:    $URL  (Workers page: the worker should be Online with each share ready)"
    echo "  Code:       $REPO_DIR"
    if [ "$MODE" = docker ]; then
        echo "  Worker:     $DIR  (.env, docker-compose.yml; work folder $WORK_DIR)"
        echo "  Logs:       cd $DIR && docker compose logs -f      (or SyncHub's Workers page, Logs button)"
        echo "  Restart:    cd $DIR && docker compose restart"
    else
        echo "  Worker:     $AGENT_DIR, settings in $INI_FILE (work folder $WORK_DIR)"
        echo "  Logs:       journalctl -u synchub-agent -f      (or SyncHub's Workers page, Logs button)"
        echo "  Restart:    sudo systemctl restart synchub-agent"
    fi
    echo "  Update:     the Update button on SyncHub's Workers page (or automatically); no SSH needed. By hand: run this script again."
    if [ -n "$GPU_FOUND" ]; then echo "  GPU:        the check above lists the encoders that really work (a test frame is encoded with each). On SyncHub's Workers page, Run benchmark compares CPU and GPU speed and quality; \"GPU encodes at once\" (Edit worker) keeps integrated graphics from being overloaded."; fi
    echo "  Settings:   Workers page, More, Edit name, tags, settings (encoder, GPU limit, update window...). No .env editing."
    printf '%s================================================================%s\n' "$BOLD" "$OFF"
}

main() {
    parse_args "$@"
    become_root "$@"
    TMP="$(mktemp -d)"
    SHARES=0; HAS_SMB=0; HAS_NFS=0; UNMOUNTED=0
    [ "$DRY_RUN" != 1 ] || printf '%sDRY RUN: nothing will be changed.%s\n' "$BOLD" "$OFF"

    step "Reading your settings"
    load_previous_settings
    say "SyncHub: $URL   Mode: $MODE   Files: $DIR   Work folder: $WORK_DIR"

    step "Installing the tools to talk to SyncHub and GitHub"
    ensure_commands git:git curl:curl

    step "Asking SyncHub for this worker's settings and shares"
    choose_code_location
    fetch_settings

    step "Installing dependencies"
    install_dependencies

    step "Getting the latest SyncHub code from GitHub"
    sync_code

    step "Updating the shared file systems (/etc/fstab) and mounting the shares"
    write_credentials
    update_fstab
    mount_shares

    step "Writing the worker's settings (.env) and starting it"
    if [ "$MODE" = docker ]; then write_docker_files; start_docker; else write_native_files; start_native; fi

    step "Checking the worker"
    verify

    step "Installing the mount watchdog and the update timer"
    install_extras

    summary
    [ "$WARNINGS" -eq 0 ] || exit 3
}

# Tests load the functions without running anything:  SYNCHUB_SETUP_SOURCED=1 source setup-worker.sh
if [ -z "${SYNCHUB_SETUP_SOURCED:-}" ]; then main "$@"; fi
