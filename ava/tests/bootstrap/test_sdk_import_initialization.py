"""Parent import completes launched SDK composition before functions escape."""

import sys
from pathlib import Path

import ava
from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("child_sdk_probes.py")


def test_launched_from_imports_share_boot_and_keep_runtime_resources_cold() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD), "launched_imports_share_boot"],
        cwd=ava.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_non_agent_import_keeps_existing_unbound_context_contract() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD), "non_agent_import_unbound_context"],
        cwd=ava.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
