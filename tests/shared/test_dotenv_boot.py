"""_env_path derives this unit's .env from AVA_HOME, so co-located gateway /
runner units each load their own .env. resolve_ava_home is the checkout-anchored
resolver that decides which home a bare invocation uses (and whether it is
anchored, i.e. safe to load the host .env's prod database URL)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared import dotenv_boot
from shared.dotenv_boot import _env_path, resolve_ava_home

# resolve_ava_home reads os.environ["AVA_HOME"] LIVE (it runs before Settings is
# built and decides the home), so these tests drive the real input via
# setitem/delitem on os.environ — not monkeypatch.setenv, which the lint bans for
# Settings fields because it can't reach the module-load Settings singleton (a
# different mechanism that does not apply to this pre-Settings resolver).


@pytest.fixture(autouse=True)
def _restore_authority_env() -> Iterator[None]:
    """`_enforce_cluster_env_authority` mutates os.environ directly (pop /
    force-assign) — not through monkeypatch, which only restores keys it set.
    Since Phase C the drop covers every cluster-scope alias key, so a test
    whose temp .env omits one cluster-scope alias deletes a value
    conftest planted, and every later test sees the field default instead.
    Snapshot the keys the authority pass touches and restore them after each
    test."""
    from shared.env_registry import (
        agent_runner_cluster_aliases,
        cluster_scope_aliases,
        env_authority_drop_set,
        env_identity_keys,
        env_keep_set,
    )

    touched = (
        cluster_scope_aliases()
        | env_identity_keys()
        | agent_runner_cluster_aliases()
        | env_authority_drop_set("gateway")
        | env_authority_drop_set("agent")
        | env_keep_set("gateway")
        | env_keep_set("agent")
        # load_dotenv also installs legacy names from the file. They are not
        # registered aliases, but a later load translates any leaked value.
        | {alias for pair in dotenv_boot._LEGACY_INVERTED_BOOL_ALIASES for alias in pair}
    )
    # Snapshot EVERY touched key (absent = None, restored as a pop): the
    # authority pass and the legacy-alias translation can ADD a touched key
    # (e.g. _translate_legacy_skip_aliases writes the canonical key), and a
    # key added by a test must not leak into the next one.
    snapshot = {k: os.environ.get(k) for k in touched}
    yield
    for key, val in snapshot.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def test_env_path_follows_ava_home() -> None:
    assert _env_path("/srv/.ava_gateway") == Path("/srv/.ava_gateway/.env")


def test_env_path_defaults_to_home_ava() -> None:
    assert _env_path(None) == Path.home() / ".ava" / ".env"


# ── resolve_ava_home: checkout-anchored home + anchored flag ──


def test_resolve_explicit_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit AVA_HOME is honored verbatim and counts as anchored — this is
    the path gateway-launched subprocesses and prod sessions take."""
    monkeypatch.setitem(os.environ, "AVA_HOME", "/srv/.ava_gateway")
    home, anchored = resolve_ava_home()
    assert home == Path("/srv/.ava_gateway")
    assert anchored is True


def test_resolve_prod_source_anchors_to_dot_ava(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod source checkout (~/.ava/source) resolves to ~/.ava with no pointer."""
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    prod = (Path.home() / ".ava" / "source").resolve()
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: prod)
    home, anchored = resolve_ava_home()
    assert home == Path.home() / ".ava"
    assert anchored is True


def test_resolve_pointer_file_anchors_to_its_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dev checkout carrying a .ava_home pointer resolves to the pointed home."""
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    (tmp_path / ".ava_home").write_text(f"{tmp_path}/.ava-mycluster\n")
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path)
    home, anchored = resolve_ava_home()
    assert home == tmp_path / ".ava-mycluster"
    assert anchored is True


def test_resolve_prod_source_beats_a_planted_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Precedence pin: when the checkout IS the prod source AND a `.ava_home`
    pointer has been planted in it, the prod anchor wins — the checkout still
    resolves to ~/.ava. A 'pointer is more specific, prefer it' refactor would
    let a seeded pointer silently repoint production (the phantom-cluster
    incident class); this test makes that regression loud."""
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    prod = tmp_path / "source"
    prod.mkdir()
    (prod / ".ava_home").write_text(f"{tmp_path}/.ava-evil\n")
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: prod)
    monkeypatch.setattr(dotenv_boot, "_prod_source", lambda: prod)
    home, anchored = resolve_ava_home()
    assert home == Path.home() / ".ava"
    assert anchored is True


def test_resolve_unanchored_dev_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A dev checkout with no explicit AVA_HOME and no pointer falls back to ~/.ava
    but is flagged UNANCHORED — the one case that must not silently take the prod
    database URL."""
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path)
    home, anchored = resolve_ava_home()
    assert home == Path.home() / ".ava"
    assert anchored is False


def test_resolve_empty_pointer_is_unanchored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blank pointer file does not anchor — treated as no pointer."""
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    (tmp_path / ".ava_home").write_text("   \n")
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path)
    _, anchored = resolve_ava_home()
    assert anchored is False


# ── resolve_ava_home: AVA_HOME contradicting the checkout's own claim ──
#
# tests/conftest.py exports AVA_HOME_OVERRIDE=1 for the whole suite (its scratch
# home IS a deliberate contradiction on any installed worktree), so every test
# below that expects a refusal has to drop it first.


def _plant_pointer(monkeypatch: pytest.MonkeyPatch, checkout: Path, target: Path) -> None:
    (checkout / ".ava_home").write_text(f"{target}\n")
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: checkout)
    monkeypatch.delitem(os.environ, "AVA_HOME_OVERRIDE", raising=False)


def test_env_contradicting_the_pointer_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 2026-07-31 wedge (#1059) in miniature: a worktree whose `.ava_home`
    names its own cluster, invoked from a shell carrying another cluster's
    AVA_HOME. Resolving either way mixes two clusters (this one's `migrations/`
    against that one's database), so it raises instead."""
    _plant_pointer(monkeypatch, tmp_path, tmp_path / ".ava-worktree")
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path / ".ava-prod"))
    with pytest.raises(dotenv_boot.AvaHomeContradictionError) as excinfo:
        resolve_ava_home()
    message = str(excinfo.value)
    assert str(tmp_path / ".ava-prod") in message  # what the env said
    assert str(tmp_path / ".ava-worktree") in message  # what the checkout said
    assert str(tmp_path / ".ava_home") in message  # who said it
    assert "unset AVA_HOME" in message  # how to fix it


def test_env_agreeing_with_the_pointer_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The common worktree case — `ava start` in a checkout whose cluster env is
    already loaded — is not a contradiction and stays unchanged."""
    home = tmp_path / ".ava-worktree"
    _plant_pointer(monkeypatch, tmp_path, home)
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    assert resolve_ava_home() == (home, True)


def test_env_wins_on_a_checkout_that_claims_no_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No pointer, not the prod source: nothing to contradict, so AVA_HOME is
    honored verbatim as before. This is what keeps enrolled runners, the
    gateway's own launched daemons and fresh clones working."""
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path)
    monkeypatch.delitem(os.environ, "AVA_HOME_OVERRIDE", raising=False)
    monkeypatch.setitem(os.environ, "AVA_HOME", "/srv/.ava_gateway")
    assert resolve_ava_home() == (Path("/srv/.ava_gateway"), True)


def test_prod_source_contradicted_by_env_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The claim can come from the path rule instead of a pointer file: the prod
    source checkout owns ~/.ava, so an AVA_HOME naming anything else is the same
    contradiction — and the error has to say where the claim came from, since
    there is no file to go read."""
    prod = tmp_path / "source"
    prod.mkdir()
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: prod)
    monkeypatch.setattr(dotenv_boot, "_prod_source", lambda: prod)
    monkeypatch.delitem(os.environ, "AVA_HOME_OVERRIDE", raising=False)
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path / ".ava-elsewhere"))
    with pytest.raises(dotenv_boot.AvaHomeContradictionError, match="prod source checkout"):
        resolve_ava_home()


def test_override_authorizes_a_deliberate_contradiction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AVA_HOME_OVERRIDE is the escape hatch the suite itself rides on: the
    contradiction resolves to the env home, unchanged from before the guard."""
    _plant_pointer(monkeypatch, tmp_path, tmp_path / ".ava-worktree")
    monkeypatch.setitem(os.environ, "AVA_HOME_OVERRIDE", "1")
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path / ".ava-scratch"))
    assert resolve_ava_home() == (tmp_path / ".ava-scratch", True)


def test_override_set_to_a_false_value_does_not_authorize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AVA_HOME_OVERRIDE=false` reads as "off" and must not open the hatch —
    a bare-presence check would turn every attempt to disable it into a bypass."""
    _plant_pointer(monkeypatch, tmp_path, tmp_path / ".ava-worktree")
    monkeypatch.setitem(os.environ, "AVA_HOME_OVERRIDE", "false")
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path / ".ava-prod"))
    with pytest.raises(dotenv_boot.AvaHomeContradictionError):
        resolve_ava_home()


def test_unnormalized_paths_are_not_a_contradiction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same home spelled two ways (`~` vs `$HOME`, a trailing `.`, a symlinked
    tmpdir) is agreement, not contradiction — the comparison resolves both sides.
    On macOS this is not hypothetical: /tmp is a symlink to /private/tmp."""
    home = tmp_path / ".ava-worktree"
    home.mkdir()
    _plant_pointer(monkeypatch, tmp_path, home)
    monkeypatch.setitem(os.environ, "AVA_HOME", f"{home}/./")
    resolved, anchored = resolve_ava_home()
    assert anchored is True
    assert resolved.resolve() == home.resolve()


def test_empty_pointer_leaves_env_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A blank pointer file claims nothing (same rule as the unanchored case
    above), so it cannot contradict an explicit AVA_HOME."""
    (tmp_path / ".ava_home").write_text("   \n")
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path)
    monkeypatch.delitem(os.environ, "AVA_HOME_OVERRIDE", raising=False)
    monkeypatch.setitem(os.environ, "AVA_HOME", "/srv/.ava_gateway")
    assert resolve_ava_home() == (Path("/srv/.ava_gateway"), True)


# ── _enforce_cluster_env_authority: this cluster's .env wins over a polluted parent ──


# The suite's own cluster-identity keys — the test process's "unit .env"
# declarations. `_point_env_at` merges them into the pointed-at env file so
# `_enforce_cluster_env_authority` takes its FORCE branch for them (a declared
# key) rather than its DROP branch (which would delete them from the process
# environment for the rest of the session — Settings() builds later in the run
# need AVA_DB_URL / AVA_REDIS_URL present).
_IDENTITY_LINES = [
    f"AVA_DB_URL={os.environ['AVA_DB_URL']}",
    f"AVA_REDIS_URL={os.environ['AVA_REDIS_URL']}",
    f"AVA_CLUSTER_SECRET={os.environ['AVA_CLUSTER_SECRET']}",
    f"AVA_GATEWAY_URL={os.environ['AVA_GATEWAY_URL']}",
]


def _point_env_at(monkeypatch: pytest.MonkeyPatch, env_file: Path, tmp_path: Path) -> None:
    merged = tmp_path / "merged.env"
    merged.write_text(
        (env_file.read_text() if env_file.exists() else "") + "\n".join(_IDENTITY_LINES) + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", merged)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")


def test_enforce_overrides_leaked_derived_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sibling cluster's health port leaked into os.environ (load_dotenv leaves an
    already-set key untouched) is corrected by this cluster's own .env — the bug
    where a preview service bound the production health port."""
    monkeypatch.setitem(os.environ, "AVA_AGENT_HOST_HEALTH_PORT", "8102")  # main's, leaked
    env_file = tmp_path / ".env"
    env_file.write_text("AVA_AGENT_HOST_HEALTH_PORT=18035\n")
    _point_env_at(monkeypatch, env_file, tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_AGENT_HOST_HEALTH_PORT"] == "18035"


def test_enforce_ignores_non_enforced_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only the derived + identity keys are forced from the file — an
    unrelated env var in the file never overrides the live environment.
    AVA_LLM_OVERRIDE is deliberately chosen: an e2e-only override read straight
    from os.environ, not a Settings field, so it has no owning sub-model to
    enforce (a Settings-backed key like AVA_LABELER_MODEL would be forced)."""
    monkeypatch.setitem(os.environ, "AVA_LLM_OVERRIDE", "env-live")
    env_file = tmp_path / ".env"
    env_file.write_text("AVA_LLM_OVERRIDE=env-file\n")
    _point_env_at(monkeypatch, env_file, tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_LLM_OVERRIDE"] == "env-live"


def test_enforce_forces_identity_key_declared_in_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A machine-identity key the unit's own .env declares wins verbatim over an
    inherited value — same authority rule as the DERIVED cluster keys. A unit's
    machine identity (serve flags, name) is a per-unit fact; the .env is its
    home."""
    monkeypatch.setitem(os.environ, "AVA_MACHINE_NAME", "leaked-prod-host")
    env_file = tmp_path / ".env"
    env_file.write_text("AVA_MACHINE_NAME=file-host\n")
    _point_env_at(monkeypatch, env_file, tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_MACHINE_NAME"] == "file-host"


def test_enforce_drops_undeclared_identity_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An inherited machine-identity key the unit's .env does NOT declare is
    dropped — the #771 leak: prod's AVA_MACHINE_SERVE_GATEWAY=true riding a
    login shell into an isolated child flipped `config_source_is_local()` to
    True, skipped the settings-lite placeholders, and the DERIVED drop then
    failed Settings with Field required. Dropped, the resolver falls through to
    the unit's own `$AVA_HOME/machine_*` files / False."""
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER", "true")
    monkeypatch.setitem(os.environ, "AVA_MACHINE_NAME", "leaked-prod-host")
    monkeypatch.setitem(os.environ, "AVA_MEMORY_REMOTE", "git@github.com:prod/AvaMemory.git")
    _point_env_at(monkeypatch, tmp_path / "no-identity.env", tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert "AVA_MACHINE_SERVE_GATEWAY" not in os.environ
    assert "AVA_MACHINE_SERVE_AGENT_RUNNER" not in os.environ
    assert "AVA_MACHINE_NAME" not in os.environ
    assert "AVA_MEMORY_REMOTE" not in os.environ


def test_enforce_keeps_timezone_supplied_by_env_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An inherited AVA_TIMEZONE the unit .env does NOT declare survives the
    authority pass — the gateway-hosted schedule runner receives the cluster
    timezone from its spawn env, and dropping it left the runner on the
    America/Los_Angeles field default (2026-08-21: schedule #3 fired at PT
    midnight instead of Shanghai midnight after the 08-12 ruling). The gateway
    is the cluster's timezone authority; a pure agent-runner re-injects the
    authoritative value via /api/bootstrap at Settings build regardless."""
    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "Asia/Shanghai")
    _point_env_at(monkeypatch, tmp_path / "no-timezone.env", tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_TIMEZONE"] == "Asia/Shanghai"


def test_spawned_child_reads_forwarded_timezone_without_env_key(
    tmp_path: Path,
) -> None:
    """The 2026-08-21 incident chain, end to end: a schedule-runner child
    spawned with AVA_TIMEZONE in its env resolves settings.general.timezone to
    that value even when its unit .env does not declare the key. Before the
    never-drop exemption the authority pass popped the forwarded value and the
    America/Los_Angeles field default won — schedule #3 fired at PT midnight
    instead of Shanghai midnight."""
    child_env = dict(os.environ)
    child_env["AVA_HOME"] = str(tmp_path)  # a unit .env with no AVA_TIMEZONE
    child_env["AVA_TIMEZONE"] = "Asia/Shanghai"
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.config import settings; print(settings.general.timezone)",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Asia/Shanghai"


def test_enforce_keeps_gateway_url_supplied_by_env_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The host-scoped gateway URL keys stay exempt from the identity drop: a
    not-yet-enrolled runner (and the test suites) supply AVA_GATEWAY_URL from
    the environment alone, and popping it would silently un-configure the
    bootstrap fetch (should_fetch_from_gateway goes False)."""
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")
    monkeypatch.setitem(os.environ, "AVA_PRIMARY_GATEWAY_URL", "http://legacy-gw:8000")
    # Hand-built env file WITHOUT the gateway-URL lines: `_point_env_at`'s suite
    # merge declares AVA_GATEWAY_URL, which would take the DERIVED force branch
    # and hide what this test pins (the URL keys survive undeclared).
    env_file = tmp_path / "no-url.env"
    env_file.write_text(
        "\n".join(
            ln
            for ln in _IDENTITY_LINES
            if ln.startswith(("AVA_DB_URL=", "AVA_REDIS_URL=", "AVA_CLUSTER_SECRET="))
        )
        + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_GATEWAY_URL"] == "http://gw:8000"
    assert os.environ["AVA_PRIMARY_GATEWAY_URL"] == "http://legacy-gw:8000"


# ── _enforce_cluster_env_authority: undeclared DERIVED keys are dropped ──
#
# A DERIVED key in the unit's own .env is forced over a polluted parent env; a
# key inherited WITHOUT a .env declaration is dropped, not kept: a pure
# agent-runner's .env carries no cluster data-plane keys (AVA_DB_URL /
# AVA_REDIS_URL / AVA_APP_PORT / ... — they come from the gateway's
# /api/bootstrap at Settings build), and an inherited sibling-cluster value
# sourced into the shell would otherwise leak the sibling's secrets into every
# child process. The fallback is the bootstrap-fetched value (runner data-plane
# keys). (AVA_PGBOUNCER_PORT itself is retired — the pooler port is a registry
# fact, never an env key.)


def test_enforce_drops_app_port_absent_from_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An inherited AVA_APP_PORT with no declaration in this unit's .env is
    removed, so a sibling cluster's value cannot stand in a pure runner (the
    pure-runner case: cluster data-plane keys arrive via bootstrap)."""
    monkeypatch.setitem(os.environ, "AVA_APP_PORT", "3001")
    _point_env_at(monkeypatch, tmp_path / "no-port.env", tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert "AVA_APP_PORT" not in os.environ


def test_enforce_keeps_app_port_declared_in_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway's own .env declaration wins verbatim over an inherited value —
    the cluster-isolation authority rule unchanged."""
    monkeypatch.setitem(os.environ, "AVA_APP_PORT", "3001")
    env_file = tmp_path / ".env"
    env_file.write_text("AVA_APP_PORT=18113\n")
    _point_env_at(monkeypatch, env_file, tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_APP_PORT"] == "18113"


def test_enforce_drops_every_undeclared_derived_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The absent-key drop is general, not pgbouncer-only: any DERIVED
    data-plane key this unit's .env does not declare is removed from the
    environment, so a sibling cluster's value sourced into the shell
    (AVA_APP_PORT / AVA_EVENTS_CHANNEL / ...) cannot stand in a pure runner's
    process. The gateway's bootstrap fetch re-injects the real values at
    Settings build. (Identity keys — AVA_DB_URL / AVA_CLUSTER_SECRET /
    AVA_GATEWAY_URL — are exempt: the suite's own .env merge declares them, see
    `_point_env_at`.)"""
    monkeypatch.setitem(os.environ, "AVA_APP_PORT", "3001")
    monkeypatch.setitem(os.environ, "AVA_EVENTS_CHANNEL", "ava:events:leaked")
    monkeypatch.setitem(os.environ, "AVA_MILVUS_PORT", "19530")
    _point_env_at(monkeypatch, tmp_path / "no-port.env", tmp_path)
    dotenv_boot._enforce_cluster_env_authority()
    assert "AVA_APP_PORT" not in os.environ
    assert "AVA_EVENTS_CHANNEL" not in os.environ
    assert "AVA_MILVUS_PORT" not in os.environ


def test_enforce_keeps_unanchored_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The unanchored sentinel is exempt from the drop: a fresh dev checkout
    plants it deliberately before the load, and popping it would silently
    un-anchor the checkout (Settings would then fail on the no-default field
    instead of failing with the named sentinel).

    Uses a hand-built env file WITHOUT the identity merge: the sentinel only
    exists in the environment, and the point of the test is that the drop
    leaves it alone."""
    monkeypatch.setitem(os.environ, "AVA_DB_URL", dotenv_boot.UNANCHORED_DB_SENTINEL)
    # Declare every OTHER identity key (the suite's .env merge in
    # `_point_env_at` does this too) so the drop pass leaves them alone: the
    # run's later Settings() builds need AVA_REDIS_URL present, and a pop here
    # would not be undone by monkeypatch (it only restores keys it set).
    env_file = tmp_path / "no-db.env"
    env_file.write_text(
        "AVA_AGENT_HOST_HEALTH_PORT=18035\n"
        + "\n".join(
            ln
            for ln in _IDENTITY_LINES
            if ln.startswith(("AVA_REDIS_URL=", "AVA_CLUSTER_SECRET=", "AVA_GATEWAY_URL="))
        )
        + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_DB_URL"] == dotenv_boot.UNANCHORED_DB_SENTINEL


def test_enforce_keeps_boot_redis_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F-s4-6: the install-time AVA_REDIS_URL placeholder is exempt from the
    drop, exactly like the DB sentinel — install plants it before the first
    settings import on a home that has no .env yet, and the authority pass
    popping it would make the documented boot mechanism a lie (before this fix
    the two placeholders were treated asymmetrically and the redis one was dead
    code)."""
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", dotenv_boot.BOOT_REDIS_PLACEHOLDER)
    env_file = tmp_path / "no-redis.env"
    env_file.write_text(
        "AVA_AGENT_HOST_HEALTH_PORT=18035\n"
        + "\n".join(
            ln
            for ln in _IDENTITY_LINES
            if ln.startswith(("AVA_DB_URL=", "AVA_CLUSTER_SECRET=", "AVA_GATEWAY_URL="))
        )
        + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_REDIS_URL"] == dotenv_boot.BOOT_REDIS_PLACEHOLDER


def test_enforce_keeps_unanchored_sentinel_over_a_declaring_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sentinel outranks an `.env` that DOES declare AVA_DB_URL.

    This is the case that matters and the one the test above cannot reach. An
    unanchored checkout resolves AVA_ENV_PATH to the DEFAULT home, so the file
    this pass reads is production's `.env` — which declares AVA_DB_URL. The
    force-assign loop therefore overwrote the sentinel `load_ava_env` had just
    planted, and the drop loop's guard never saw it. Measured on a real
    unanchored checkout before the fix: the process came out of boot holding the
    production database URL, which is precisely what the sentinel exists to
    prevent.

    Other declared cluster keys must still be forced from the file — the guard is
    about the sentinel, not about disabling the authority pass."""
    monkeypatch.setitem(os.environ, "AVA_DB_URL", dotenv_boot.UNANCHORED_DB_SENTINEL)
    monkeypatch.setitem(os.environ, "AVA_EVENTS_CHANNEL", "leaked-from-a-sibling")
    env_file = tmp_path / "declares-db.env"
    env_file.write_text(
        "AVA_DB_URL=postgresql://someone_else@127.0.0.1:5433/not_ours\n"
        "AVA_EVENTS_CHANNEL=ava:thisunit:events\n"
        "AVA_AGENT_HOST_HEALTH_PORT=18035\n"
        + "\n".join(
            ln
            for ln in _IDENTITY_LINES
            if ln.startswith(("AVA_REDIS_URL=", "AVA_CLUSTER_SECRET=", "AVA_GATEWAY_URL="))
        )
        + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")

    dotenv_boot._enforce_cluster_env_authority()

    assert os.environ["AVA_DB_URL"] == dotenv_boot.UNANCHORED_DB_SENTINEL
    assert os.environ["AVA_EVENTS_CHANNEL"] == "ava:thisunit:events", (
        "the authority pass must still force every other declared cluster key"
    )


# ── Per-process profile: gateway pop vs agent/runner survival ──


def test_enforce_drops_a_leaked_redis_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The exemption is placeholder-EXACT: a sibling unit's real AVA_REDIS_URL
    leaked in via the parent env is still dropped when this unit's .env does
    not declare one."""
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", "redis://sibling:6379/0")
    env_file = tmp_path / "no-redis.env"
    env_file.write_text(
        "AVA_AGENT_HOST_HEALTH_PORT=18035\n"
        + "\n".join(
            ln
            for ln in _IDENTITY_LINES
            if ln.startswith(("AVA_DB_URL=", "AVA_CLUSTER_SECRET=", "AVA_GATEWAY_URL="))
        )
        + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")
    dotenv_boot._enforce_cluster_env_authority()
    assert "AVA_REDIS_URL" not in os.environ


# ── Per-process profile: gateway pop vs agent/runner survival ──


@pytest.mark.parametrize(
    "profile,key_should_survive",
    [
        # profile=gateway → agent-runner cluster keys are popped
        ("gateway", False),
        # profile=agent → agent-runner cluster keys survive (agent needs them)
        ("agent", True),
        # profile=runner → agent-runner cluster keys survive (daemon needs them)
        ("runner", True),
        # no profile → no pop (bare checkout / test / script)
        (None, True),
    ],
)
def test_gateway_profile_pops_only_for_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: str | None,
    key_should_survive: bool,
) -> None:
    """Agent-runner cluster-scoped keys are popped from os.environ ONLY when
    AVA_PROCESS_PROFILE=gateway. Agent processes (profile=agent), runner daemons
    (profile=runner), and unprofiled processes (tests, bare checkouts) keep them.

    This is the regression test for the bug where _is_gateway_process() used
    AVA_MACHINE_SERVE_GATEWAY (a unit capability flag true on every single-box
    process) instead of AVA_PROCESS_PROFILE (an explicit process-type marker).
    """
    # Pick a representative agent-runner cluster-scoped key that is in
    # AGENT_RUNNER_CLUSTER_ALIASES. DEEPSEEK_API_KEY is cluster-pinned
    # agent-runner — the gateway must NOT carry it in os.environ.
    test_key = "DEEPSEEK_API_KEY"
    monkeypatch.setitem(os.environ, test_key, "sk-test-value")

    if profile is not None:
        monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", profile)
    else:
        monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)

    # The .env file needs the identity keys so _enforce_cluster_env_authority
    # takes its FORCE branch and doesn't wipe them. DEEPSEEK_API_KEY is declared
    # too — a single-box unit's .env carries every cluster field (install writes
    # them all), so the FORCE branch keeps it in every profile and the ONLY thing
    # that can remove it is the gateway profile pop. (An undeclared cluster key
    # is dropped for every profile — that is the F-s4-4 authority rule, not the
    # profile pop this test guards.)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(_IDENTITY_LINES) + f"\n{test_key}=sk-test-value\n")
    _point_env_at(monkeypatch, env_file, tmp_path)

    # The gateway pop removes EVERY agent-runner cluster alias from os.environ
    # (AVA_EXEC_TIMEOUT_SECONDS among them). monkeypatch only restores keys IT
    # set, so without this snapshot the worker's os.environ loses those keys
    # for the rest of the session — the D5 config fallback cache then snapshots
    # the field default (AVA_EXEC_TIMEOUT_SECONDS=300) and every later
    # config-service read in this worker serves it (2026-08-06 CI flake).
    from shared.env_registry import agent_runner_cluster_aliases

    _pre_pop = {k: os.environ.get(k) for k in agent_runner_cluster_aliases()}

    try:
        dotenv_boot._enforce_cluster_env_authority()

        if key_should_survive:
            assert test_key in os.environ, (
                f"DEEPSEEK_API_KEY should survive for profile={profile!r} (agent/runner/CLI need it)"
            )
            assert os.environ[test_key] == "sk-test-value"
        else:
            assert test_key not in os.environ, (
                "DEEPSEEK_API_KEY should be popped for profile=gateway "
                "(gateway process does not need agent keys)"
            )
    finally:
        # Restore the aliases the gateway pop removed, so the worker's
        # os.environ survives this test intact (monkeypatch would restore only
        # the keys IT set — AVA_EXEC_TIMEOUT_SECONDS etc. would stay gone and
        # poison the D5 config fallback cache for the whole session).
        for _k, _v in _pre_pop.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v


# ── Labeler boot: profile-less spawn keeps the LLM provider keys (task #1230) ──


def test_labeler_boot_without_profile_marker_reads_provider_key_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: the labeler's session env (the gateway session-forward
    allowlist — cluster-scope provider keys deliberately NOT forwarded, and no
    AVA_PROCESS_PROFILE marker after the ops.spec opt-out, task #1230) boots
    against a unit .env that declares DEEPSEEK_API_KEY; the key must resolve
    through the DeepSeek provider plugin's DEEPSEEK_API_KEY environment seam.

    This is the regression test for the labeler's RuntimeError('DEEPSEEK_API_KEY
    not set') retry loop: the .env re-source (F-s4-4 force branch) restores the
    key, and the gateway-profile pop must NOT remove it again — the pop only
    runs for processes carrying AVA_PROCESS_PROFILE=gateway, and the labeler's
    spec no longer sets it."""
    # The labeler's session env carries no provider key and no profile marker.
    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    had_key = "DEEPSEEK_API_KEY" in os.environ
    old_key = os.environ.get("DEEPSEEK_API_KEY")
    monkeypatch.delitem(os.environ, "DEEPSEEK_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-labeler-test\n")
    _point_env_at(monkeypatch, env_file, tmp_path)

    try:
        dotenv_boot.load_ava_env()

        assert os.environ["DEEPSEEK_API_KEY"] == "sk-labeler-test"

        # A fresh (profile-less) Settings reads the key — the exact read the
        # labeler daemon performs via build_chat_model -> plugin require_key.
        from shared.config import Settings

        fresh = Settings(profile=None)
        assert fresh.lm.deepseek_api_key is not None
        assert fresh.lm.deepseek_api_key.get_secret_value() == "sk-labeler-test"
    finally:
        # The authority pass force-assigned the key from the temp .env; the
        # autouse _restore_authority_env fixture only restores keys that existed
        # before the test, so restore the prior state by hand (a leaked key here
        # flips test_config's bootstrap completeness assertions in the same run).
        if had_key and old_key is not None:
            os.environ["DEEPSEEK_API_KEY"] = old_key
        else:
            os.environ.pop("DEEPSEEK_API_KEY", None)


def test_gateway_marker_still_drops_provider_keys_for_other_daemons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mirror image: a daemon that DOES carry AVA_PROCESS_PROFILE=gateway
    (heartbeat, events-maintenance, im-bridge, ...) still loses the provider
    keys at boot — the labeler's opt-out must not spread the keys into every
    gateway-profile daemon (anti-spread, task #1230)."""
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "gateway")
    monkeypatch.delitem(os.environ, "DEEPSEEK_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-labeler-test\n")
    _point_env_at(monkeypatch, env_file, tmp_path)

    dotenv_boot.load_ava_env()

    assert "DEEPSEEK_API_KEY" not in os.environ


def test_agent_profile_keeps_an_unmodeled_provider_key_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The authority pass leaves a plugin key in the agent parent's live env.

    Provider bindings deliberately do not add Settings fields, so this is the
    prerequisite for `child_env("agent", ...)` to forward their declared key.
    """
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "agent")
    # Register the original absence before load_ava_env adds this key directly.
    monkeypatch.setitem(os.environ, "TESTP_API_KEY", "before-load")
    monkeypatch.delitem(os.environ, "TESTP_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TESTP_API_KEY=sk-plugin-test\n")
    _point_env_at(monkeypatch, env_file, tmp_path)

    dotenv_boot.load_ava_env()

    assert os.environ["TESTP_API_KEY"] == "sk-plugin-test"


def test_agent_profile_keeps_launcher_runner_url_and_drops_admin_passwords(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway's .env holds the owner URL and admin passwords, but an agent
    must retain the launcher's runner projection and scrub every unprojected
    data-plane password."""
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setitem(os.environ, "AVA_DB_ADMIN_PASSWORD", "db-admin-only")
    monkeypatch.setitem(os.environ, "AVA_REDIS_ADMIN_PASSWORD", "redis-admin-only")
    monkeypatch.setitem(os.environ, "AVA_REDIS_PASSWORD", "redis-runtime-only")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AVA_DB_URL=postgresql://ava:owner-password@127.0.0.1:5433/ava\n"
        "AVA_DB_ADMIN_PASSWORD=db-admin-only\n"
        "AVA_REDIS_ADMIN_PASSWORD=redis-admin-only\n"
        "AVA_REDIS_PASSWORD=redis-runtime-only\n"
    )
    _point_env_at(monkeypatch, env_file, tmp_path)
    monkeypatch.setitem(
        os.environ,
        "AVA_DB_URL",
        "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava",
    )

    dotenv_boot._enforce_cluster_env_authority()

    assert os.environ["AVA_DB_URL"].startswith("postgresql://ava_runner:")
    assert "AVA_DB_ADMIN_PASSWORD" not in os.environ
    assert "AVA_REDIS_ADMIN_PASSWORD" not in os.environ
    assert "AVA_REDIS_PASSWORD" not in os.environ


# ─── legacy inverted AVA_SKIP_* alias translation ───


def test_translate_legacy_skip_aliases_inverts(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_SKIP_AUTH=true meant "skip auth" — the canonical key must receive the
    INVERTED value; when the canonical key is present it wins and nothing moves."""
    monkeypatch.delitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", raising=False)
    monkeypatch.setitem(os.environ, "AVA_SKIP_AUTH", "true")
    monkeypatch.delitem(os.environ, "AVA_SECURITY_SCAN_ENABLED", raising=False)
    monkeypatch.setitem(os.environ, "AVA_SKIP_SECURITY_SCAN", "false")

    dotenv_boot._translate_legacy_skip_aliases()

    assert os.environ["AVA_AUTH_MIDDLEWARE_ENABLED"] == "false"
    assert os.environ["AVA_SECURITY_SCAN_ENABLED"] == "true"

    # Canonical present -> no translation, canonical survives verbatim. Raw
    # assignment, not monkeypatch.setitem: setitem would record the
    # translation-written "false" as the "original" and re-add it at teardown
    # AFTER the module's restore fixture popped it (leak into later tests).
    os.environ["AVA_AUTH_MIDDLEWARE_ENABLED"] = "true"
    dotenv_boot._translate_legacy_skip_aliases()
    assert os.environ["AVA_AUTH_MIDDLEWARE_ENABLED"] == "true"


def test_load_ava_env_translates_legacy_skip_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: a .env carrying the legacy keys yields the translated canonical
    keys in os.environ after load_ava_env (the authority pass drops the
    undeclared canonical cluster alias first, then the translation fills it in)."""
    monkeypatch.delitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", raising=False)
    monkeypatch.delitem(os.environ, "AVA_SECURITY_SCAN_ENABLED", raising=False)
    monkeypatch.delitem(os.environ, "AVA_SKIP_AUTH", raising=False)
    monkeypatch.delitem(os.environ, "AVA_SKIP_SECURITY_SCAN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("AVA_SKIP_AUTH=true\nAVA_SKIP_SECURITY_SCAN=false\n")
    _point_env_at(monkeypatch, env_file, tmp_path)
    dotenv_boot.load_ava_env()
    assert os.environ["AVA_AUTH_MIDDLEWARE_ENABLED"] == "false"
    assert os.environ["AVA_SECURITY_SCAN_ENABLED"] == "true"
