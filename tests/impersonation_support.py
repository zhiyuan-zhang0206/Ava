"""Attestation fixtures for impersonation tests.

Control commands carry no credential; they are authorized by caller presence
against the session's recorded controller tree. These helpers build a recorded
tree with one provider anchor and callers that attest (or do not) against it.
"""

from typing import Any


def recorded_tree() -> dict[str, Any]:
    """A recorded controller tree with one provider anchor (codex)."""
    return {
        "pid": 4242,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        "created_at": 1000.0,
        "parent_pid": 4241,
        "ancestors": [
            {
                "pid": 4241,
                "name": "zsh",
                "executable": "/bin/zsh",
                "created_at": 999.0,
                "parent_pid": 4240,
            },
            {
                "pid": 4240,
                "name": "codex",
                "executable": "/opt/codex",
                "created_at": 998.0,
                "parent_pid": 1,
            },
        ],
    }


def attested_caller(lease: dict[str, Any]) -> dict[str, Any]:
    """Caller metadata that descends from the lease's recorded tree."""
    recorded = lease["process_metadata"]
    chain = [
        {
            "pid": recorded["pid"],
            "name": recorded["name"],
            "executable": recorded["executable"],
            "created_at": recorded["created_at"],
        }
    ]
    chain.extend(dict(node) for node in recorded["ancestors"])
    return {
        "pid": 5000,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        "created_at": 2000.0,
        "parent_pid": recorded["pid"],
        "ancestors": chain,
    }


def unrelated_caller() -> dict[str, Any]:
    """Caller metadata from a process tree with no provider anchor."""
    return {
        "pid": 6000,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        "created_at": 3000.0,
        "parent_pid": 1,
        "ancestors": [],
    }
