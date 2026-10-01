#!/bin/sh
# A stand-in for the wal-g binary, driven only by its --config file's directory:
#
#   <dir>/mode        "ok" (default), "fail" (every call exits 1), "hang" (every call
#                     blocks until the mode file changes or disappears), "nodelete"
#                     (`st rm` is refused) or "corrupt" (`st get` returns other bytes)
#   <dir>/store/      the object store: `wal-push` copies a segment to store/<basename>,
#                     `st put/ls/get/rm` read and write store/ (put adds ".lz4", like wal-g)
#   <dir>/basebackups/<name>/  a base backup the test took (e.g. `pg_basebackup`): `backup-fetch
#                     DIR <name>` copies it into DIR (`LATEST` is the last by name); a
#                     `wal-fetch SEGMENT DEST` copies store/SEGMENT, exit 74 when it is missing
#   <dir>/calls.log   one line per call: the arguments after --config
#   <dir>/env.log     `backup-push` only: the environment the tick handed it
#   <dir>/fail-commands  space-separated command names (backup-list, backup-push,
#                     wal-verify, delete) that exit 1; every other command works
#   <dir>/backups.json   what `backup-list` prints (default: one backup, name only);
#                     `backup-push` replaces it with backups-after.json when that exists
#   <dir>/wal-verify.json, wal-verify.rc   what `wal-verify` prints and its exit code (default 0)
#   <dir>/delete-dry.log, delete-confirm.log  WAL-G's log (stderr) of `delete`
#                     without and with --confirm
#
# Not a test module. It needs no environment: the test and Postgres' archive
# command both reach it through `--config <dir>/walg.json`.
set -eu

if [ "${1:-}" != "--config" ] || [ $# -lt 3 ]; then
    echo "fake wal-g: usage: fake_walg.sh --config FILE COMMAND..." >&2
    exit 2
fi
config=$2
shift 2
dir=$(dirname "$config")
store="$dir/store"
mkdir -p "$store"
echo "$*" >> "$dir/calls.log"

mode=$(cat "$dir/mode" 2>/dev/null || echo ok)
case "$mode" in
    fail)
        echo "fake wal-g: simulated failure" >&2
        exit 1
        ;;
    hang)
        while [ "$(cat "$dir/mode" 2>/dev/null || echo ok)" = hang ]; do
            sleep 0.2
        done
        ;;
esac

command=$1
shift
for failing in $(cat "$dir/fail-commands" 2>/dev/null || true); do
    if [ "$failing" = "$command" ]; then
        echo "fake wal-g: simulated failure of $command" >&2
        exit 1
    fi
done
case "$command" in
    --version)
        echo "wal-g version v3.0.9 fake"
        ;;
    backup-list)
        if [ -f "$dir/backups.json" ]; then
            cat "$dir/backups.json"
        else
            echo '[{"backup_name":"base_000000010000000000000002"}]'
        fi
        ;;
    backup-push)
        echo "PGHOST=${PGHOST:-} PGPORT=${PGPORT:-} PGUSER=${PGUSER:-} WALG_DELTA_MAX_STEPS=${WALG_DELTA_MAX_STEPS:-}" >> "$dir/env.log"
        if [ -f "$dir/backups-after.json" ]; then
            cp "$dir/backups-after.json" "$dir/backups.json"
        fi
        ;;
    wal-verify)
        cat "$dir/wal-verify.json"
        exit "$(cat "$dir/wal-verify.rc" 2>/dev/null || echo 0)"
        ;;
    delete)
        log=delete-dry.log
        for arg in "$@"; do
            if [ "$arg" = "--confirm" ]; then
                log=delete-confirm.log
            fi
        done
        cat "$dir/$log" >&2
        ;;
    wal-push)
        cp "$1" "$store/$(basename "$1")"
        ;;
    wal-fetch)
        if [ ! -f "$store/$1" ]; then
            echo "fake wal-g: $1 is not in the store" >&2
            exit 74
        fi
        cp "$store/$1" "$2"
        ;;
    backup-fetch)
        name=$2
        if [ "$name" = LATEST ]; then
            name=$(ls "$dir/basebackups" | sort | tail -1)
        fi
        if [ ! -d "$dir/basebackups/$name" ]; then
            echo "fake wal-g: no such backup $name" >&2
            exit 1
        fi
        cp -R "$dir/basebackups/$name/." "$1"
        ;;
    st)
        sub=$1
        shift
        case "$sub" in
            put)
                mkdir -p "$store/$(dirname "$2")"
                cp "$1" "$store/$2.lz4"
                ;;
            ls)
                (cd "$store" && find "${1:-.}" -type f 2>/dev/null | sed 's|^\./||' | sort)
                ;;
            get)
                if [ "$mode" = corrupt ]; then
                    echo "not what was written" > "$2"
                else
                    cp "$store/$1" "$2"
                fi
                ;;
            rm)
                if [ "$mode" = nodelete ]; then
                    echo "fake wal-g: AccessDenied: delete refused" >&2
                    exit 1
                fi
                rm -f "$store/$1"
                ;;
            *)
                echo "fake wal-g: unknown st command $sub" >&2
                exit 2
                ;;
        esac
        ;;
    *)
        echo "fake wal-g: unknown command $command" >&2
        exit 2
        ;;
esac
