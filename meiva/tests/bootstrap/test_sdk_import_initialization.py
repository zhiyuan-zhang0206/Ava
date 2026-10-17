import sys
from pathlib import Path

from base.host.proc import run_bounded
from meiva.sdk_surface import process_context

_CHILD = Path(__file__).with_name("child_sdk_probes.py")


def test_non_agent_import_keeps_existing_unbound_context_contract() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD), "non_agent_import_unbound_context"],
        cwd=process_context.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
