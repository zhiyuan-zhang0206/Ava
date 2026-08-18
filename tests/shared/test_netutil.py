"""`shared/netutil.py` — the config-free loopback + IPv4-literal predicates."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.netutil import is_ipv4_literal, is_loopback_host


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "127.0.0.1", "127.1.2.3", "::1", "[::1]", "0.0.0.0", " localhost "],  # noqa: S104 — test data, classifying addresses, not binding
)
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["100.64.0.87", "10.0.0.5", "runner.tailnet.ts.net", "example.com", "", "128.0.0.1"],
)
def test_non_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is False


def test_netutil_imports_without_config() -> None:
    """netutil must not pull shared.config (which builds Settings at import and
    fails on a fresh host) — enroll depends on this."""
    import ast

    src = Path(__file__).resolve().parents[2] / "shared" / "netutil.py"
    tree = ast.parse(src.read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(m.startswith("shared.config") for m in imported), imported


@pytest.mark.parametrize(
    "host",
    ["100.64.0.72", "127.0.0.1", "0.0.0.0", "10.0.0.5", "1.2.3.4", " 1.2.3.4 "],  # noqa: S104 — test data, classifying addresses, not binding
)
def test_ipv4_literal_hosts(host: str) -> None:
    assert is_ipv4_literal(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "example.com",
        "runner.tailnet.ts.net",
        "",
        "::1",
        "[::1]",
        "2607:7700:0:54::6467:6048",
        "[2607:7700:0:54::6467:6048]",
        "999.1.1.1",  # out-of-range octet — not a valid literal
        "1.2.3",  # incomplete
    ],
)
def test_non_ipv4_literal_hosts(host: str) -> None:
    assert is_ipv4_literal(host) is False
