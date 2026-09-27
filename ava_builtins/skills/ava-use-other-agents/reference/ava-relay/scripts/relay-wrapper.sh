#!/bin/bash
# Ava impersonation relay — session-lifetime wrapper (task #4612).
#
# Loaded by the ava-relay session plugin for every claude takeover launched in
# resident mode. Waits for the launcher-scoped credential stub, consumes it
# once, then execs the relay for the rest of the session. stdout stays pure:
# relay events only; diagnostics go to the log file beside the stub.
set -u

STUB="${AVA_IMPERSONATION_RELAY_STUB:-}"
RELAY_PY="${AVA_IMPERSONATION_RELAY_PY:-}"
LOG="${STUB%.env}.log"

if [ -z "$STUB" ] || [ -z "$RELAY_PY" ]; then
  echo "ava-relay: launcher env missing (AVA_IMPERSONATION_RELAY_STUB/RELAY_PY); wrapper idle-exit" >>"${TMPDIR:-/tmp}/ava-relay-wrapper.log"
  exit 0
fi

echo "ava-relay: wrapper start pid=$$ at $(date -u +%FT%TZ)" >>"$LOG"

# The request flow writes the credential stub inside the session; wait (bounded).
WAIT_SECONDS="${AVA_IMPERSONATION_RELAY_STUB_WAIT_SECONDS:-900}"
for _ in $(seq 1 "$WAIT_SECONDS"); do
  [ -s "$STUB" ] && break
  sleep 1
done

if [ ! -s "$STUB" ]; then
  echo "ava-relay: no credential stub after ${WAIT_SECONDS}s; wrapper exits (session unaffected)" >>"$LOG"
  exit 0
fi

# Consume once: never let a later session pick up a stale credential.
set -a
# shellcheck disable=SC1090
. "$STUB"
set +a
# Mark consumption before removing the stub, so another request never sees
# both handoff paths empty while this one-time wrapper is already committed.
echo $$ >"${STUB%.env}.pid"
rm -f "$STUB"

echo "ava-relay: stub consumed sid=${SID:-?} agent=${AGENT:-?}; exec relay at $(date -u +%FT%TZ)" >>"$LOG"
exec "$RELAY_PY" -m cli impersonate relay "$AGENT" --session "$SID" --provider claude
