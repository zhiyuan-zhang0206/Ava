#!/usr/bin/env bash
# tcc-preauth.sh v2 — read-only macOS TCC diagnostics for AvaPermissionsHelper.
#
# Provenance: Task #2421 audit deliverable, originally created for workspace #5805.
# v2 makes the stable helper app the only authorization subject and removes the
# experimental user TCC.db write and tccd-restart paths. Direct TCC.db writes are
# unverified on macOS 26, and Python is no longer the responsible TCC process.
#
# This script is read-only. It checks the helper process, calls the repository
# client ping, attempts sqlite3 -readonly TCC queries, and prints the manual grants.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repo_root
readonly python_bin="$repo_root/.venv/bin/python"
readonly helper_bundle_id="com.ava.permissions-helper"
readonly user_tcc="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
readonly system_tcc="/Library/Application Support/com.apple.TCC/TCC.db"
host_name=$(hostname -s 2>/dev/null || hostname)
readonly host_name

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

status_line() {
    local check=$1
    local status=$2
    local detail=$3
    printf 'HOST=%s\tCHECK=%s\tSTATUS=%s\tDETAIL=%s\n' \
        "$host_name" "$check" "$status" "$detail"
}

q() {
    local database=$1
    local service=$2
    local indirect=${3:-}
    local predicate
    local result

    if [[ -n "$indirect" ]]; then
        predicate="AND indirect_object_identifier='$indirect'"
    else
        predicate="AND (indirect_object_identifier IS NULL OR indirect_object_identifier='UNUSED')"
    fi
    result=$(sqlite3 -readonly "$database" \
        "SELECT auth_value FROM access WHERE service='$service' AND client='$helper_bundle_id' $predicate LIMIT 1;")
    if [[ -n "$result" ]]; then
        printf '%s\n' "$result"
    else
        printf '%s\n' '(none)'
    fi
}

auth_status() {
    case "$1" in
        2) printf '%s\n' GRANTED ;;
        0) printf '%s\n' DENIED ;;
        3) printf '%s\n' LIMITED ;;
        '(none)') printf '%s\n' NOT_RECORDED ;;
        *) printf '%s\n' UNKNOWN ;;
    esac
}

report_tcc_service() {
    local database=$1
    local scope=$2
    local service=$3
    local label=$4
    local indirect=${5:-}
    local value
    local status

    if ! value=$(q "$database" "$service" "$indirect" 2>/dev/null); then
        status_line "tcc-$label" UNKNOWN \
            "$scope TCC.db query failed; Full Disk Access is required to read the database, so verify in System Settings"
        return
    fi
    status=$(auth_status "$value")
    status_line "tcc-$label" "$status" "$scope auth_value=$value"
}

verify_tcc() {
    if [[ -r "$user_tcc" ]] && sqlite3 -readonly "$user_tcc" 'SELECT 1;' >/dev/null 2>&1; then
        status_line tcc-user-db READABLE 'read-only query succeeded'
        report_tcc_service "$user_tcc" user kTCCServiceSystemPolicyDesktopFolder desktop-folder
        report_tcc_service "$user_tcc" user kTCCServiceSystemPolicyDocumentsFolder documents-folder
        report_tcc_service "$user_tcc" user kTCCServiceSystemPolicyDownloadsFolder downloads-folder
        report_tcc_service "$user_tcc" user kTCCServiceCamera camera
        report_tcc_service "$user_tcc" user kTCCServiceMicrophone microphone
        report_tcc_service "$user_tcc" user kTCCServiceAppleEvents appleevents-safari com.apple.Safari
        report_tcc_service "$user_tcc" user kTCCServiceAppleEvents appleevents-terminal com.apple.Terminal
        report_tcc_service "$user_tcc" user kTCCServiceAppleEvents appleevents-finder com.apple.finder
        report_tcc_service "$user_tcc" user kTCCServiceAppleEvents appleevents-chrome com.google.Chrome
        report_tcc_service "$user_tcc" user kTCCServiceAppleEvents appleevents-systemevents com.apple.systemevents
    else
        status_line tcc-user-db UNKNOWN \
            'Full Disk Access is required to read the database; verify grants in System Settings'
    fi

    if [[ -r "$system_tcc" ]] && sqlite3 -readonly "$system_tcc" 'SELECT 1;' >/dev/null 2>&1; then
        status_line tcc-system-db READABLE 'read-only query succeeded'
        report_tcc_service "$system_tcc" system kTCCServiceSystemPolicyAllFiles full-disk-access
        report_tcc_service "$system_tcc" system kTCCServiceScreenCapture screen-recording
        report_tcc_service "$system_tcc" system kTCCServiceAccessibility accessibility
    else
        status_line tcc-system-db UNKNOWN \
            'Full Disk Access is required to read the database; verify grants in System Settings'
    fi
}

verify_helper_process() {
    local pids
    local pid_list

    if ! pids=$(pgrep -f 'AvaPermissionsHelper.app/Contents/MacOS/AvaPermissionsHelper'); then
        status_line helper-process DOWN 'no AvaPermissionsHelper process found'
        return 1
    fi
    pid_list=$(printf '%s\n' "$pids" | paste -sd, -)
    status_line helper-process UP "pids=$pid_list"
}

verify_helper_ping() {
    local output
    local pong
    local screen
    local accessibility
    local detail
    local result=0

    if ! output=$(
        cd "$repo_root"
        "$python_bin" - 2>&1 <<'PY'
from services.permissions_helper.client import ping

reply = ping()
print(
    int(reply["pong"] is True),
    int(reply["preflight_screen"] is True),
    int(reply["ax_trusted"] is True),
    sep="\t",
)
PY
    ); then
        detail=$(printf '%s' "$output" | tr '\n' ' ')
        status_line helper-ping DOWN "repository client ping failed: $detail"
        return 1
    fi

    IFS=$'\t' read -r pong screen accessibility <<<"$output"
    if [[ "$pong" == 1 ]]; then
        status_line helper-ping UP 'pong=true'
    else
        status_line helper-ping DOWN 'pong=false'
        result=1
    fi
    if [[ "$screen" == 1 ]]; then
        status_line screen-recording GRANTED 'preflight_screen=true'
    else
        status_line screen-recording MISSING 'preflight_screen=false'
        result=1
    fi
    if [[ "$accessibility" == 1 ]]; then
        status_line accessibility GRANTED 'ax_trusted=true'
    else
        status_line accessibility MISSING 'ax_trusted=false'
        result=1
    fi
    return "$result"
}

list_manual() {
    cat <<'EOF'

Manual one-time grants for AvaPermissionsHelper:
1. Full Disk Access
   System Settings > Privacy & Security > Full Disk Access > AvaPermissionsHelper
2. Screen Recording
   System Settings > Privacy & Security > Screen & System Audio Recording > AvaPermissionsHelper
   Earlier macOS releases label this pane "Screen Recording."
3. Accessibility
   System Settings > Privacy & Security > Accessibility > AvaPermissionsHelper
4. Desktop, Documents, and Downloads
   System Settings > Privacy & Security > Files & Folders > AvaPermissionsHelper
5. Camera and Microphone, only when an agent needs them
   System Settings > Privacy & Security > Camera > AvaPermissionsHelper
   System Settings > Privacy & Security > Microphone > AvaPermissionsHelper
6. AppleEvents automation for Safari, Terminal, Finder, Chrome, and System Events
   System Settings > Privacy & Security > Automation > AvaPermissionsHelper

All grants belong to the stable AvaPermissionsHelper.app identity, never to a
Python interpreter. This script does not modify TCC.db or restart tccd.
EOF
}

verify() {
    local result=0

    [[ "$(uname -s)" == Darwin ]] || fail 'tcc-preauth.sh supports macOS only'
    [[ -x "$python_bin" ]] || fail "repository Python is missing: $python_bin"
    command -v sqlite3 >/dev/null 2>&1 || fail 'sqlite3 is required for read-only TCC queries'

    printf 'Read-only Ava TCC verification\n'
    if ! verify_helper_process; then
        result=1
    fi
    if ! verify_helper_ping; then
        result=1
    fi
    verify_tcc
    list_manual
    return "$result"
}

usage() {
    printf 'usage: %s [--verify|--list-manual]\n' "$0"
}

case "${1:---verify}" in
    --verify) verify ;;
    --list-manual) list_manual ;;
    -h | --help) usage ;;
    *) usage >&2; exit 2 ;;
esac
