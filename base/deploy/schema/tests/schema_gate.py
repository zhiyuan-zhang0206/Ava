"""Keep a daemon shutdown child independent of the live cluster schema gate."""

from base.host.tests.daemon_shutdown_child import DaemonModule, run_wedged_daemon


def _noop_schema_gate(*_args: object, **_kwargs: object) -> None:
    """Stand-in for ``assert_schema_current``: the check needs a live cluster."""


def run_child(mod: DaemonModule) -> None:
    """Run a statically selected daemon with the existing hermetic schema gate."""
    import base.deploy.schema.migrations

    # This gate is orthogonal to signal wiring and would dial the cluster DB.
    base.deploy.schema.migrations.assert_schema_current = _noop_schema_gate
    run_wedged_daemon(mod)
