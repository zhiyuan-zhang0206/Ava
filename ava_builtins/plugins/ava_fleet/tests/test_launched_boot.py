import sys
from pathlib import Path

from ava.sdk_surface import process_context
from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("child_launched_boot.py")


def test_launched_from_imports_share_boot_and_keep_runtime_resources_cold() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD)],
        cwd=process_context.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
