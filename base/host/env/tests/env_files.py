from pathlib import Path

import pytest

__all__ = ["skip_authority_pass", "use_env_files"]


def skip_authority_pass(_home: Path) -> None:
    pass


def use_env_files(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, mirror_file: Path | None = None
) -> Path:
    home = env_file.parent / f"{env_file.stem}-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_file.read_text())
    if mirror_file is not None and mirror_file.exists():
        (home / "mirror.env").write_text(mirror_file.read_text())
    monkeypatch.setenv("MEIVA_HOME", str(home))
    return home
