"""Where cluster-extension materialization is invoked from, and why not converge.

The registry read has a precondition: this cluster's Postgres is up AND its
schema is current. `ava start` satisfies that only late in its sequence:

    1)   converge host state
    2)   gateway brings up THIS cluster's pg/redis
    2.5) apply pending migrations
    2.6) verify schema version
    2.7) materialize cluster extensions   <- the first point the precondition holds

So a `CONVERGE_STEPS` entry — which the design doc originally described, and
which #201 shipped — runs before the database exists on a single box, and before
the `extensions` table exists on the rollout that creates it. It would have
failed on every start of the most common posture while printing a warning that
reads like a transient outage.

These pin the placement, because it is invisible at the call site: the function
looks equally correct in either home, and only the surrounding sequence says
which one works.
"""

from __future__ import annotations

import inspect


def test_materialization_is_not_a_converge_step() -> None:
    """Converge runs before the data plane is up (`ava start` step 1 vs step 2),
    so nothing in `CONVERGE_STEPS` may require the cluster database."""
    from cli.commands._converge import CONVERGE_STEPS

    offenders = [s.name for s in CONVERGE_STEPS if "extension" in s.name.lower()]
    assert not offenders, (
        f"{offenders} is a converge STEP, but converge runs before this cluster's "
        "Postgres is started and before migrations apply. Call it from the start "
        "sequence after the schema check instead — see cli/commands/_converge_extensions.py:"
        "materialize_cluster_extensions."
    )


def test_start_materializes_after_the_schema_check() -> None:
    """The ordering inside `ava start`, asserted on the real source.

    Brittle-looking on purpose: the invariant IS the order of two calls, and
    nothing else in the process can observe it. If someone moves the
    materialization above the migration apply, the registry read starts hitting
    a schema that may predate the `extensions` table, and the symptom is a
    warning that looks transient rather than a failure.
    """
    from cli.commands.start import _cmd_start_body

    src = inspect.getsource(_cmd_start_body)
    migrate_at = src.index("cmd_migrations_apply()")
    schema_at = src.index("_assert_schema_current_or_die()")
    materialize_at = src.index("materialize_cluster_extensions()")

    assert migrate_at < materialize_at, (
        "materialization must run AFTER pending migrations apply — otherwise the "
        "rollout that creates the extensions table reads it before it exists"
    )
    assert schema_at < materialize_at, (
        "materialization must run AFTER the schema-current check — reading the "
        "registry against a schema this checkout does not understand is exactly "
        "what that check exists to prevent"
    )


def test_standalone_converge_materializes_too() -> None:
    """`ava converge` is run against a cluster that is already up, so it has the
    precondition the start path has to wait for — and an operator running it
    expects the machine to end up caught up."""
    from cli.commands._converge import cmd_converge

    assert "materialize_cluster_extensions()" in inspect.getsource(cmd_converge)


def test_the_materializer_lives_beside_its_siblings() -> None:
    """`_converge_extensions.py`, next to `_converge_skills.py` — a subsystem
    reader, not another entry in `_converge.py`'s host-state roster. Extracting
    it is also what kept `_converge.py` under the 800-line ceiling."""
    from cli.commands import _converge_extensions

    assert hasattr(_converge_extensions, "materialize_cluster_extensions")


def test_start_adopts_before_it_materializes() -> None:
    """Both orders are correct, and one of them is tidier.

    An unclaimed local name is invisible to the materializer (it has no row) and
    a freshly adopted one hashes as `unchanged`, so neither order can produce a
    wrong result. Sweeping first means a single pass leaves this machine and the
    cluster agreeing; materializing first leaves the machine one converge behind
    on the names it just uploaded.
    """
    from cli.commands.start import _cmd_start_body

    src = inspect.getsource(_cmd_start_body)
    assert src.index("adopt_local_extensions()") < src.index("materialize_cluster_extensions()")


def test_standalone_converge_adopts_too() -> None:
    """`ava converge` is what an operator runs to make a machine correct without
    restarting it, and a machine holding un-adopted installs is not correct."""
    from cli.commands._converge import cmd_converge

    assert "adopt_local_extensions()" in inspect.getsource(cmd_converge)
