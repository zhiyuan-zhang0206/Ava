#!/bin/sh
# A stand-in for the wal-g binary, driven only by its --config file's directory:
#
#   <dir>/mode        "ok" (default), "fail" (every call exits 1) or "hang" (every call
#                     blocks until the mode file changes or disappears)
#   <dir>/store/      the object store: `wal-push` copies a segment to store/<basename>,
#                     `st put/ls/get/rm` read and write store/ (put adds ".lz4", like wal-g)
#   <dir>/calls.log   one line per call: the arguments after --config
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
case "$command" in
    --version)
        echo "wal-g version v3.0.9 fake"
        ;;
    backup-list)
        echo '[{"backup_name":"base_000000010000000000000002"}]'
        ;;
    wal-push)
        cp "$1" "$store/$(basename "$1")"
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
                cp "$store/$1" "$2"
                ;;
            rm)
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
