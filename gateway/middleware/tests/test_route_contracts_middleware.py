"""The pause-exempt surface is audited and the pause policy agrees with it."""

from __future__ import annotations

from gateway.middleware import pause_policy

_EXPECTED_CONTROL_PLANE = frozenset(
    {
        ("POST", "/api/cluster/stopping"),
        ("GET", "/api/cluster/status"),
        ("GET", "/api/cluster/roster"),
        ("GET", "/api/cluster/admin/events"),
        ("GET", "/api/cluster/machines"),
        ("DELETE", "/api/cluster/machines/{name}"),
        ("POST", "/api/cluster/machines/{name}/staging"),
        ("POST", "/api/cluster/machines/{name}/pause"),
        ("POST", "/api/cluster/machines/{name}/resume"),
        ("POST", "/api/alerts"),
        ("POST", "/api/work-failed"),
        ("GET", "/api/health"),
        ("GET", "/api/bootstrap"),
    }
)


def test_pause_exempt_surface_is_audited() -> None:
    """The exempt surface is exactly the reviewed set — no silent additions.

    This is the "enumerable and auditable" property of doorplate ②: a new
    exemption must be a deliberate edit to this test + the contract, never
    an invisible middleware string.
    """
    assert pause_policy.control_plane_surface() == _EXPECTED_CONTROL_PLANE


def test_should_bypass_pause_agrees_with_surface() -> None:
    """The decision function answers by the declared surface, on concrete
    request paths (templates must match real paths)."""
    exempt = [
        ("GET", "/api/health"),
        ("GET", "/api/cluster/status"),
        ("POST", "/api/cluster/stopping"),
        ("POST", "/api/alerts"),
        ("GET", "/api/bootstrap"),
    ]
    blocked = [
        ("GET", "/api/alerts"),  # same template as the exempt webhook, different method
        ("GET", "/api/alerts/stream"),
        ("GET", "/api/agents"),
        ("GET", "/api/agents/42/messages"),
        ("GET", "/api/agents/42"),
        ("GET", "/api/cluster/status/extra"),  # exact match, not prefix
        ("POST", "/api/cluster/update"),  # retired route: no pause exemption survives it
        ("POST", "/api/cluster/recover"),  # retired route: no pause exemption survives it
        ("GET", "/pages/5-report/a/b"),
    ]
    for method, path in exempt:
        assert pause_policy.should_bypass_pause(method, path), f"expected exempt: {method} {path}"
    for method, path in blocked:
        assert not pause_policy.should_bypass_pause(method, path), (
            f"expected blocked: {method} {path}"
        )
