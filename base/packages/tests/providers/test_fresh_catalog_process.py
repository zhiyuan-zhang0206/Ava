"""The explicitly built plugin catalog works in a fresh process (budget and model withdrawal)."""

import subprocess
import sys
from pathlib import Path

from base.config import settings

_FRESH_CATALOG_CHILD = Path(__file__).with_name("_fresh_catalog_child.py")


def test_owned_catalog_resolves_budget_in_a_fresh_process() -> None:
    """A cold root explicitly builds the catalog before resolving a budget."""
    model = settings.lm.llm_model
    result = subprocess.run(  # noqa: S603 — our own venv python + a fixed child script
        [sys.executable, str(_FRESH_CATALOG_CHILD), "budget", model],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0


def test_explicit_catalog_resolves_withdrawal_in_a_fresh_process() -> None:
    """The composition root builds a catalog; synthetic changes stay local."""
    result = subprocess.run(  # noqa: S603 — our own venv python + a fixed child script
        [sys.executable, str(_FRESH_CATALOG_CHILD), "withdrawal"],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
