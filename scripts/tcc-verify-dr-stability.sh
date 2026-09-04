#!/usr/bin/env bash
set -euo pipefail

requested_workdir=${1:-/tmp/tcc-dr-verify}
case "$requested_workdir" in
    /tmp/*) ;;
    *)
        echo "FAIL: workdir must be a child of /tmp" >&2
        exit 2
        ;;
esac

mkdir -p "$requested_workdir"
workdir=$(cd "$requested_workdir" && pwd -P)
case "$workdir" in
    /tmp/* | /private/tmp/*) ;;
    *)
        echo "FAIL: resolved workdir escaped /tmp: $workdir" >&2
        exit 2
        ;;
esac

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source_file="$workdir/main.swift"
info_plist="$repo/services/permissions_helper/helper/Info.plist"
app="$workdir/app/AvaPermissionsHelper.app"
exe="$app/Contents/MacOS/AvaPermissionsHelper"
cert_cn="Ava Permissions Helper Code Signing"
bundle_id="com.ava.permissions-helper"

identity_output=$(security find-identity -p codesigning)
sha1=$(
    printf '%s\n' "$identity_output" |
        sed -En 's/^[[:space:]]*[0-9]+\)[[:space:]]+([0-9A-Fa-f]{40})[[:space:]]+"Ava Permissions Helper Code Signing".*$/\1/p' |
        sed -n '1p' |
        tr '[:upper:]' '[:lower:]'
)
if [[ ! "$sha1" =~ ^[0-9a-f]{40}$ ]]; then
    echo "FAIL: code-signing identity is missing or its name does not match" >&2
    exit 1
fi
expected_dr="identifier \"$bundle_id\" and certificate leaf = H\"$sha1\""

cp "$repo/services/permissions_helper/helper/main.swift" "$source_file"
mkdir -p "$(dirname "$exe")"
cp "$info_plist" "$app/Contents/Info.plist"

sign_and_read_dr() {
    codesign \
        --force \
        --sign "$cert_cn" \
        --identifier "$bundle_id" \
        --requirements "=designated => $expected_dr" \
        "$app"
    codesign -d -r- "$app" 2>&1 | sed -n 's/^designated => //p'
}

swiftc -O "$source_file" -o "$exe"
first_dr=$(sign_and_read_dr)
if [[ "$first_dr" != "$expected_dr" ]]; then
    echo "FAIL: first designated requirement differs from expected" >&2
    printf 'expected: %s\nactual:   %s\n' "$expected_dr" "$first_dr" >&2
    exit 1
fi
echo "PASS: first build uses the pinned designated requirement"

touch "$source_file"
swiftc -O "$source_file" -o "$exe"
second_dr=$(sign_and_read_dr)
if [[ "$second_dr" != "$expected_dr" ]]; then
    echo "FAIL: rebuilt designated requirement differs from expected" >&2
    printf 'expected: %s\nactual:   %s\n' "$expected_dr" "$second_dr" >&2
    exit 1
fi
echo "PASS: rebuilt app uses the pinned designated requirement"

if [[ "$first_dr" != "$second_dr" ]]; then
    echo "FAIL: designated requirement changed across rebuilds" >&2
    exit 1
fi
echo "PASS: designated requirements are byte-for-byte identical across rebuilds"
echo "workdir: $workdir"
