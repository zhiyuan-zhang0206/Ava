"""Fresh-process scenarios for the gateway bootstrap tests.

Usage: `child_gateway_scenarios.py <scenario> [args]`.

- `checkout_move <source> <repo>`: import the gateway's first-load image from a
  throwaway checkout, move that checkout's HEAD, and print what the image reports.
- `server_shutdown`: run the real gateway server child for the SIGTERM tests.
"""

import json
import subprocess
import sys
from pathlib import Path


def _checkout_move(source: str, repo: str) -> None:
    sys.path[:0] = [source, repo]
    from base.native_process.code_version import CodeVersion
    from gateway.cluster import process_boot

    loaded = process_boot.LOADED_IMAGE
    root = Path(source)
    first = loaded.sha
    (root / "moved").write_text("new checkout")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=entry-proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    repeated = process_boot.LOADED_IMAGE
    current = subprocess.check_output(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    )
    sys.stdout.write(
        json.dumps(
            {
                "first": first,
                "after": repeated.sha,
                "same": repeated is loaded,
                "current": current.strip(),
                "version": CodeVersion(loaded).get(),
            }
        )
        + "\n"
    )


def _server_shutdown() -> None:
    from gateway.tests.bootstrap.test_server_shutdown import _run_child

    _run_child()


if __name__ == "__main__":
    if sys.argv[1] == "checkout_move":
        _checkout_move(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "server_shutdown":
        _server_shutdown()
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")
