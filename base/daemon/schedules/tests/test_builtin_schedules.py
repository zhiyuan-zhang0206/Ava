"""Tests for base/daemon/schedules/builtin_schedules.py — the built-in schedules manifest
and its idempotent create-if-missing provisioning.

The provision path is also exercised at the gateway-boot level by
test_schedules_api.py's TestClient(app) lifespan; these tests pin the
manifest parsing + DB behavior directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.daemon.schedules.builtin_schedules import ManifestError, load_manifest


def _manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    """Write a fixture manifest with one tiny script per entry."""
    for e in entries:
        script = e["script"]
        assert isinstance(script, str)
        (tmp_path / script).write_text("print('ok')\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 1, "builtin_schedules": entries})
    )
    return tmp_path / "manifest.json"


class TestLoadManifest:
    def test_loads_repo_manifest(self) -> None:
        """The repo manifest parses: product schedules enabled / operator schedules disabled."""
        scheds = load_manifest()
        by_name = {s.name: s for s in scheds}
        assert set(by_name) == {
            "c9-daily-report",
            "debt-sweep-daily",
            "dev-ci-metrics",
            "adversarial-eval-weekly",
            "self-evolution-weekly",
            "self-evolution-daily",
            "memory-arbiter",
            "model-update-tracker",
            "trace-ship-tempo",
        }
        assert all(
            s.klass == "product" and s.default_enabled
            for s in scheds
            if s.name != "trace-ship-tempo"
        )
        assert by_name["trace-ship-tempo"].klass == "operator"
        assert by_name["trace-ship-tempo"].default_enabled is False

    def test_unknown_class_fails_fast(self, tmp_path: Path) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "x",
                    "class": "mystery",
                    "default_enabled": True,
                    "script": "x.py",
                    "command": "python x.py",
                }
            ],
        )
        with pytest.raises(ManifestError, match="unknown class"):
            load_manifest(path)

    def test_missing_script_file_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(
            json.dumps(
                {
                    "builtin_schedules": [
                        {
                            "name": "x",
                            "class": "product",
                            "default_enabled": True,
                            "script": "nope.py",
                            "command": "python nope.py",
                        }
                    ]
                }
            )
        )
        with pytest.raises(ManifestError, match="does not exist"):
            load_manifest(path)
