from base.host.tests.daemon_shutdown_child import DaemonModule, run_wedged_daemon


def _noop_schema_gate(*_args: object, **_kwargs: object) -> None:
    pass


def run_child(mod: DaemonModule) -> None:
    import base.deploy.schema.migrations

    base.deploy.schema.migrations.assert_schema_current = _noop_schema_gate
    run_wedged_daemon(mod)
