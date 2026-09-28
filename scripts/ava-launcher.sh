#!/bin/sh
# The host's bare `ava` (~/.local/bin/ava links here): run the CLI of the cluster
# that $AVA_HOME names. Each cluster links its own CLI at $AVA_HOME/ava when it
# is converged (`ava start` / `ava converge`), so the environment alone decides
# the cluster: PATH is rebuilt by every login shell, AVA_HOME is inherited as is.
# There is no default cluster and no fallback: without AVA_HOME, or when that
# home has no CLI link, this exits 2 and runs nothing.
if [ -z "${AVA_HOME:-}" ]; then
    echo "ava: AVA_HOME is not set. Export the home of the cluster to act on" >&2
    echo "  (export AVA_HOME=<cluster home>), or run that checkout's .venv/bin/ava." >&2
    exit 2
fi
if [ ! -x "$AVA_HOME/ava" ]; then
    echo "ava: $AVA_HOME/ava does not exist. A cluster links its CLI there when it" >&2
    echo "  starts; run that checkout's .venv/bin/ava start (or ava converge) first." >&2
    exit 2
fi
exec "$AVA_HOME/ava" "$@"
