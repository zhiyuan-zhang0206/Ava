from pathlib import Path

from cli.commands._converge_pitr import converge_pitr_foundation
from cli.commands._converge_spec import ConvergeCtx


def test_converge_publishes_self_contained_private_shim(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    source = repo / "services" / "pitr" / "archive_shim.py"
    source.parent.mkdir(parents=True)
    source.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    home = tmp_path / "home"

    converge_pitr_foundation(ConvergeCtx(repo=repo, ava_home=home, roles=frozenset({"gateway"})))

    shim = home / "runtime" / "pg-archive" / "archive-shim"
    assert shim.read_bytes() == source.read_bytes()
    assert shim.stat().st_mode & 0o777 == 0o700
    assert (home / "physical-backup" / "spool").stat().st_mode & 0o777 == 0o700
