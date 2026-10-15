import sys
import time
from pathlib import Path

from base.sessions.backend import get_shell_backend
from base.sessions.pty import client


def _stay_alive() -> None:
    sys.stdout.write("created\n")
    sys.stdout.flush()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    scenario = sys.argv[1]
    if scenario == "agent_host":
        client.create_session(
            sys.argv[2], sys.argv[3], {}, sys.argv[4], config=client.PtyClientConfig()
        )
        _stay_alive()
    elif scenario == "backend_host":
        assert get_shell_backend().new_session(sys.argv[2], "", Path(sys.argv[3]), env={})
        _stay_alive()
    elif scenario == "send":
        client.send(sys.argv[2], sys.argv[3].encode() + b"\r")
    else:
        raise SystemExit(f"unknown scenario {scenario!r}")
