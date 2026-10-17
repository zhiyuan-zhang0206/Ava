"""Parity: the boot-lite index's parse path equals the eager sub-model construction.

Each case sets one env alias and compares, for the same field: the lite
`_lite.resolve()` result against constructing the owning eager sub-model — value
for value, and (for rejected values) error class for error class. Cases span
every parse kind the manifest declares (bool / int / float / path / csv / iana /
port / str), valid and invalid. Runs in a subprocess because a pytest process
boots eager (tests/fixtures/env_bootstrap.py) while these paths exist only in the default
boot-lite mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]

# (field, env alias, raw value) — one exercise per kind and per failure mode.
_CASES: list[tuple[str, str, str]] = [
    ("llm_model", "MEIVA_MODEL", "parity-model"),
    ("db_pool_acquire_timeout_seconds", "MEIVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "5"),
    ("db_pool_acquire_timeout_seconds", "MEIVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "2.5"),
    ("db_pool_acquire_timeout_seconds", "MEIVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "abc"),
    ("gateway_client_retry_delay_seconds", "MEIVA_GATEWAY_RETRY_DELAY_SECONDS", "1.5"),
    ("eval_isolation", "MEIVA_EVAL_ISOLATION", "yes"),
    ("eval_isolation", "MEIVA_EVAL_ISOLATION", "0"),
    ("eval_isolation", "MEIVA_EVAL_ISOLATION", "TRUE"),
    ("eval_isolation", "MEIVA_EVAL_ISOLATION", "bogus"),
    ("eval_network_allowlist", "MEIVA_EVAL_NETWORK_ALLOWLIST", "a.com, b.com"),
    ("eval_network_allowlist", "MEIVA_EVAL_NETWORK_ALLOWLIST", "not-a-host,ok.com"),
    ("timezone", "MEIVA_TIMEZONE", "Asia/Shanghai"),
    ("timezone", "MEIVA_TIMEZONE", "Bogus/Zone"),
    ("telemetry_otlp_port", "MEIVA_TELEMETRY_OTLP_PORT", "3200"),
    ("telemetry_otlp_port", "MEIVA_TELEMETRY_OTLP_PORT", "70000"),
    ("machine_serve_gateway", "MEIVA_MACHINE_SERVE_GATEWAY", "true"),
]

_CHILD = Path(__file__).with_name("_lite_parity_child.py")


def _run_child(home: Path) -> list[dict[str, Any]]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.pop("VIRTUAL_ENV", None)
    env["MEIVA_HOME"] = str(home)
    env["MEIVA_CONFIG_FETCH"] = "skip"
    env["PARITY_CASES"] = json.dumps(_CASES)
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", str(_CHILD)],
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    line = next(line for line in proc.stdout.splitlines() if line.startswith("PARITY "))
    return json.loads(line.removeprefix("PARITY "))


def test_lite_parity_matrix(tmp_path: Path) -> None:
    results = _run_child(tmp_path)
    assert len(results) == len(_CASES)
    for case in results:
        label = f"{case['name']}={case['raw']!r}"
        if case["eager_error"] is None:
            assert case["lite_error"] is None, f"{label}: lite rejected a valid value: {case}"
            assert case["lite_value"] == case["eager_value"], f"{label}: {case}"
        else:
            assert case["eager_is_value_error"] is True, f"{label}: eager error is not ValueError"
            assert case["lite_error"] is not None, f"{label}: lite accepted a rejected value"
            assert case["lite_error"].startswith("ValueError"), f"{label}: {case}"
            # The named validity rules name the offending alias in their message.
            if case["name"] in {"timezone", "telemetry_otlp_port", "eval_network_allowlist"}:
                assert case["alias"] in case["lite_error"], f"{label}: {case}"
