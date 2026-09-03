"""CI-only retained collector validate/start/loopback OTLP delivery proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from shared.paths import otel_collector_binary, otel_collector_config


def require(condition: bool, message: str) -> None:  # noqa: FBT001 — assertion predicate.
    if not condition:
        raise AssertionError(message)


def main() -> None:
    if os.environ["GITHUB_ACTIONS"] != "true":
        raise RuntimeError("collector proof is CI-only")
    home = Path(os.environ["AVA_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    binary = otel_collector_binary()
    require(binary.is_relative_to(Path(sys.prefix).resolve().parent), "binary outside image")
    from cli.commands._otel_collector import ensure_otel_collector

    rejected_home = home / "must-not-be-created"
    with patch("shared.runtime_interpreter.runtime_otel_binary", return_value=home / "missing"):
        try:
            ensure_otel_collector(home / "no-source", rejected_home, None)
        except RuntimeError as exc:
            require("prepare before start" in str(exc), "wrong missing-image rejection")
        else:
            raise AssertionError("missing retained collector did not fail closed")
    require(not rejected_home.exists(), "missing-image check mutated home")
    config = otel_collector_config()
    require(config.is_relative_to(home), "config not in unit home")
    output = home / "received-traces.json"
    # All receiver/exporter paths are local; no production config is consumed.
    config.write_text(
        "receivers:\n  otlp:\n    protocols:\n      http:\n"
        "        endpoint: 127.0.0.1:43872\n"
        f"exporters:\n  file:\n    path: {output}\n"
        "service:\n  telemetry:\n    metrics:\n      level: none\n"
        "  pipelines:\n    traces:\n      receivers: [otlp]\n      exporters: [file]\n"
    )
    subprocess.run(  # noqa: S603 — generation-verified executable and private CI config.
        [str(binary), "validate", "--config", str(config)], check=True, timeout=30
    )
    log = home / "collector-proof.log"
    with log.open("wb") as stream:
        child = subprocess.Popen(  # noqa: S603 — same retained executable, no shell.
            [str(binary), "--config", str(config)], stdout=stream, stderr=stream
        )
        try:
            payload = json.dumps(
                {
                    "resourceSpans": [
                        {
                            "scopeSpans": [
                                {
                                    "spans": [
                                        {
                                            "traceId": "12345678901234567890123456789012",
                                            "spanId": "1234567890123456",
                                            "name": "retained-runtime-proof",
                                            "kind": 1,
                                            "startTimeUnixNano": "1700000000000000000",
                                            "endTimeUnixNano": "1700000000000000001",
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ).encode()
            deadline = time.monotonic() + 30
            while True:
                if child.poll() is not None:
                    raise RuntimeError("retained collector exited before OTLP delivery")
                try:
                    request = urllib.request.Request(
                        "http://127.0.0.1:43872/v1/traces",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310 — fixed loopback HTTP URL.
                        require(response.status == 200, "OTLP receiver rejected trace")
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            while not output.exists() or "retained-runtime-proof" not in output.read_text():
                if time.monotonic() >= deadline:
                    raise RuntimeError("collector accepted OTLP but did not export the span")
                time.sleep(0.2)
        finally:
            child.terminate()  # Only this CI fixture's own exact subprocess.
            child.wait(timeout=15)
    (home.parent / "otel-proof.json").write_text(
        json.dumps(
            {
                "binary": str(binary),
                "sourceAbsent": True,
                "validate": True,
                "otlpLoopbackExport": True,
                "configOutsideImage": True,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
