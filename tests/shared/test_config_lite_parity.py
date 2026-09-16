"""Parity: the boot-lite index's parse path equals the eager sub-model construction.

Each case sets one env alias and compares, for the same field: the lite
`_lite.resolve()` result against constructing the owning eager sub-model — value
for value, and (for rejected values) error class for error class. Cases span
every parse kind the manifest declares (bool / int / float / path / csv / iana /
port / str), valid and invalid. Runs in a subprocess because a pytest process
boots eager (tests/conftest.py) while these paths exist only in the default
boot-lite mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

# (field, env alias, raw value) — one exercise per kind and per failure mode.
_CASES: list[tuple[str, str, str]] = [
    ("llm_model", "AVA_MODEL", "parity-model"),
    ("db_pool_acquire_timeout_seconds", "AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "5"),
    ("db_pool_acquire_timeout_seconds", "AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "2.5"),
    ("db_pool_acquire_timeout_seconds", "AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "abc"),
    ("gateway_client_retry_delay_seconds", "AVA_GATEWAY_RETRY_DELAY_SECONDS", "1.5"),
    ("eval_isolation", "AVA_EVAL_ISOLATION", "yes"),
    ("eval_isolation", "AVA_EVAL_ISOLATION", "0"),
    ("eval_isolation", "AVA_EVAL_ISOLATION", "TRUE"),
    ("eval_isolation", "AVA_EVAL_ISOLATION", "bogus"),
    ("eval_network_allowlist", "AVA_EVAL_NETWORK_ALLOWLIST", "a.com, b.com"),
    ("eval_network_allowlist", "AVA_EVAL_NETWORK_ALLOWLIST", "not-a-host,ok.com"),
    ("timezone", "AVA_TIMEZONE", "Asia/Shanghai"),
    ("timezone", "AVA_TIMEZONE", "Bogus/Zone"),
    ("telemetry_otlp_port", "AVA_TELEMETRY_OTLP_PORT", "3200"),
    ("telemetry_otlp_port", "AVA_TELEMETRY_OTLP_PORT", "70000"),
    ("ava_home", "AVA_HOME", "~/parity-home"),
    ("machine_serve_gateway", "AVA_MACHINE_SERVE_GATEWAY", "true"),
]

_CHILD = """import json, os
import shared.config._lite as lite
from shared.config import _full
from shared.config_lite_table import FIELD_DOMAINS
from shared.config_registry import _DOMAIN_MODELS

MODELS = {
    attr: getattr(_full, model_name)
    for attr, _label, model_name, _cap in _DOMAIN_MODELS
    if isinstance(model_name, str)
}

lite.prepare()

results = []
for name, alias, raw in json.loads(os.environ["PARITY_CASES"]):
    os.environ[alias] = raw
    try:
        lite_value = lite.resolve(name)
        lite_error = None
    except Exception as exc:  # noqa: BLE001 - the error identity IS the subject
        lite_value = None
        lite_error = f"{type(exc).__name__}: {exc}"
    model = MODELS[FIELD_DOMAINS[name]]
    try:
        eager_value = getattr(model(), name)
        eager_error = None
        eager_is_value_error = None
    except Exception as exc:  # noqa: BLE001
        eager_value = None
        eager_error = f"{type(exc).__name__}: {exc}"
        eager_is_value_error = isinstance(exc, ValueError)
    finally:
        os.environ.pop(alias, None)
    results.append(
        {
            "name": name,
            "alias": alias,
            "raw": raw,
            "lite_value": repr(lite_value),
            "lite_error": lite_error,
            "eager_value": repr(eager_value),
            "eager_error": eager_error,
            "eager_is_value_error": eager_is_value_error,
        }
    )
print("PARITY " + json.dumps(results))
"""


def _run_child() -> list[dict[str, Any]]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.pop("VIRTUAL_ENV", None)
    env["PARITY_CASES"] = json.dumps(_CASES)
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", "-c", _CHILD],
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


def test_lite_parity_matrix() -> None:
    results = _run_child()
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
