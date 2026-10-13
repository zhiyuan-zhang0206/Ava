"""Env-file setup for tests that hand-build the files a boot pass reads (standard library and pytest only)."""

from pathlib import Path

import pytest

__all__ = ["skip_authority_pass", "use_env_files"]


def skip_authority_pass(_home: Path) -> None:
    """Stand-in for `dotenv_boot._enforce_cluster_env_authority` where a test
    exercises the rest of the boot and must not have its env rewritten."""


def use_env_files(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, mirror_file: Path | None = None
) -> Path:
    """Name, through `AVA_HOME`, a home whose `.env` (and `mirror.env`) carry these
    files' content: for a test that hand-builds the files a boot pass reads.

    The home is a sibling directory of `env_file`; the variable is the only thing
    the boot reads, so nothing else needs patching."""
    home = env_file.parent / f"{env_file.stem}-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_file.read_text())
    if mirror_file is not None and mirror_file.exists():
        (home / "mirror.env").write_text(mirror_file.read_text())
    monkeypatch.setenv("AVA_HOME", str(home))
    return home
