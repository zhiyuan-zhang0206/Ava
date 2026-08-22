"""Functional readiness semantics for ``deploy/lgtm/start.sh``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("grafana_identity", "expected_returncode", "expected_text"),
    [
        ("viewer", 0, "stack is up"),
        ("anonymous", 1, "authenticated Grafana Viewer readiness failed"),
    ],
)
def test_start_requires_authenticated_grafana_readiness(
    tmp_path: Path,
    grafana_identity: str,
    expected_returncode: int,
    expected_text: str,
) -> None:
    """A listening but unauthorized/broken Grafana must not fake green."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    user_json = (
        '{"login":"ava-cluster-viewer","orgId":1,"isGrafanaAdmin":false}'
        if grafana_identity == "viewer"
        else '{"login":"Anonymous","orgId":1,"isGrafanaAdmin":false}'
    )
    for name, script in {
        "docker": "#!/bin/sh\nexit 0\n",
        "curl": (
            "#!/bin/sh\n"
            'case "$*" in *"--noproxy *"*) ;; *) exit 2 ;; esac\n'
            'case "$*" in *"-H X-Ava-Grafana-User: ava-cluster-viewer"*) ;; *) exit 2 ;; esac\n'
            'case "$*" in *"-H X-Ava-Grafana-Role: Viewer"*) ;; *) exit 2 ;; esac\n'
            'case "$*" in\n'
            f"  *'/api/user/orgs'*) printf '%s' '[{{\"orgId\":1,\"role\":\"Viewer\"}}]' ;;\n"
            f"  *'/api/user'*) printf '%s' '{user_json}' ;;\n"
            "  *'/api/search'*) printf '%s' '[]' ;;\n"
            "  *) printf '%s' '200' ;;\n"
            "esac\n"
        ),
        "sleep": "#!/bin/sh\nexit 0\n",
    }.items():
        executable = fake_bin / name
        executable.write_text(script)
        executable.chmod(0o755)

    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603 — fixed local bash + repository script
        ["bash", str(repo / "deploy" / "lgtm" / "start.sh")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AVA_LGTM_PYTHON": str(Path(sys.executable)),
            "GRAFANA_ROOT_URL": "http://gateway.test:8000/grafana/",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == expected_returncode
    assert expected_text in result.stdout + result.stderr
