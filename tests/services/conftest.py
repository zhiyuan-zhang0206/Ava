"""Collection guard for the services tests; their fixtures live in
`tests/fixtures/path_scoped/services.py`.

The ava-root skeleton ships its POSIX mechanisms first (flock, unix sockets);
elsewhere its modules cannot even import yet, so its test files are excluded from
collection there.
"""

from __future__ import annotations

import sys

_AVA_ROOT_TESTS = [
    "test_ava_root_daemon.py",
    "test_ava_root_ipc.py",
    "test_ava_root_manifest.py",
    "test_ava_root_supervisor.py",
]

collect_ignore: list[str] = []
if sys.platform == "win32":
    collect_ignore += _AVA_ROOT_TESTS
