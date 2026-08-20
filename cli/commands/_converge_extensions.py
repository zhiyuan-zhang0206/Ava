"""Land the cluster's extensions on this machine — the converge-side reader.

A sibling of `_converge_skills.py` (repo/plugin skills from the checkout) and
`_converge_plugins.py`, for the content the CLUSTER owns: whatever arrived by
`ava skill install` on any machine (`future/infra/extension-ownership.md` S2,
model in `decisions/2026-08-21-extension-ownership-three-tiers.md`).

Its own module rather than another function in `_converge.py` because it is not
a converge STEP — see the docstring below — and because `_converge.py` is a
roster of host-state steps, not a place for a subsystem's reader.
"""

from __future__ import annotations

import sys


def materialize_cluster_extensions() -> None:
    """Land the cluster's installed skills onto this machine.

    Adds the names that arrived by `ava skill install` on any machine of the
    cluster, alongside the repo/plugin skills converge already syncs from the
    checkout (a `source='repo'` row carries no blob — the schema enforces it).
    That is what makes a freshly enrolled runner fungible: no "did we remember
    to install the skills here" step.

    **Deliberately NOT a `CONVERGE_STEPS` entry**, though the design doc first
    described it as one. `ava start` runs converge as step 1, brings this
    cluster's Postgres up as step 2, and applies migrations as step 2.5 — so a
    converge step would run before the database is up on a single box, and
    before the `extensions` table exists on the rollout that creates it. It
    would have failed on every start of the most common posture while printing a
    warning that reads like a transient. Both callers below instead invoke it at
    a point where the data plane is up and migrated:
    `cmd_converge` (the cluster is already running) and `ava start` after the
    schema-current verification.

    Failures are reported, not raised. Being behind is a recoverable state with
    a retry already in place, and a cluster DB that is down should not stop a
    host from starting. This is the opposite of the install path's fail-fast,
    and deliberately so: install is the moment a fact is CREATED, where a silent
    local-only result is drift; materialization is the moment a machine catches
    up.
    """
    from shared import db, extension_materialize, paths

    try:
        with db.pool().connection() as conn:
            result = extension_materialize.materialize_skills(conn, dest_root=paths.skills_dir())
    except Exception as exc:  # see the docstring: report, never block converge
        print(f"  ! extensions: cluster registry unreachable ({exc}); skipping", file=sys.stderr)
        return
    for kind, names in (
        ("landed", result.landed),
        ("updated", result.updated),
    ):
        if names:
            print(f"    {kind}: {', '.join(names)}")
    for name in result.kept_local_edits:
        print(f"  ! extensions: kept local edits to {name} (not overwritten)", file=sys.stderr)
    for name in result.missing_blob:
        print(f"  ! extensions: {name} has no stored content — reinstall it", file=sys.stderr)


def adopt_local_extensions() -> None:
    """Upload this machine's pre-registry skill installs into the cluster.

    Runs BEFORE `materialize_cluster_extensions` on the same converge. Either
    order is correct — an unclaimed name is invisible to the materializer and an
    adopted one hashes as `unchanged` — but sweeping first means one pass leaves
    the machine and the cluster agreeing, instead of two.

    Same failure stance as its sibling and for the same reason: a machine that
    cannot reach the cluster is behind, not broken, and the next converge
    retries. The one thing worth being loud about is a name two machines
    disagree on, which `shared.extension_adopt` logs and this reports again on
    the operator's terminal — it is the only outcome here that needs a person.
    """
    from shared import db, extension_adopt, paths

    try:
        result = extension_adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())
    except Exception as exc:  # see the docstring: report, never block converge
        print(f"  ! extensions: could not adopt local installs ({exc}); skipping", file=sys.stderr)
        return
    if result.adopted:
        print(f"    adopted into the cluster: {', '.join(result.adopted)}")
    for name in result.missing_tree:
        print(
            f"  ! extensions: {name} is tracked locally but missing from disk — "
            "reinstall it or deregister it",
            file=sys.stderr,
        )
    for clash in result.conflicts:
        print(
            f"  ! extensions: {clash.name} is claimed by {clash.claimed_by} with different "
            "content than this machine holds — NOT adopted, both copies intact; install "
            "whichever is right over the other",
            file=sys.stderr,
        )
