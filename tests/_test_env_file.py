"""Test-home .env sync and early native LGTM host-env neutralization.

This plain module is imported by conftest before project Settings; the env-file
helper lives outside conftest's fixture collection.
"""

from __future__ import annotations

import os
from pathlib import Path


# PR #2481: key presence can matter. Pop the host LGTM port block before
# conftest builds the eager Settings singleton; never pin default values here.
def _neutralize_lgtm_host_env() -> None:
    for key in [key for key in os.environ if key.startswith("AVA_LGTM_")]:
        os.environ.pop(key)
    assert not any(key.startswith("AVA_LGTM_") for key in os.environ)


_neutralize_lgtm_host_env()


def rewrite_line(env_path: Path, key: str, value: str) -> None:
    """Replace (or append) one KEY=value line in a .env file, in place."""
    lines = env_path.read_text().splitlines()
    kept = [ln for ln in lines if not ln.startswith(f"{key}=")]
    kept.append(f"{key}={value}")
    env_path.write_text("\n".join(kept) + "\n")
