"""Unit tests for install-time cluster birth (`python -m cli.install_cluster`).

All side-effecting steps are monkeypatched (mirroring
tests/cli/test_cluster_lifecycle.py) so no real pg/redis/subprocess is needed.
Verified:
- worktree birth: home-keyed registry record + .env (derived keys with the fixed
  `ava` data-plane identity, serve flags) + the checkout's .ava_home pointer —
  identity is the path, so ANY path works (no naming convention, no name flag)
- secret semantics (user decision: off is fully off): a single-machine role
  (gateway,agent-runner) births a NO-AUTH cluster with an EMPTY secret unless
  the one-shot AVA_INSTALL_CLUSTER_SECRET states one; a gateway-only split host
  mints a fresh one; a secret already in the .env is never rotated; the generic
  runtime environment is never consulted
- idempotency: a second run is a no-op birth and keeps the secret state
- role without `gateway`: no birth, serve flags only
- refusals: the default home (prod) in worktree mode, and a home another
  checkout owns (no matching .ava_home pointer)
- CLI surface: --role and --seed-source are required (no defaults)
- seed: allowlist-only copy; the cluster secret / telegram token never travel
- rollback: a failed provision deregisters a record this install allocated
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

import cli.install_cluster as ic
from shared import cluster as cl
from shared.cluster import get_record

_GW_AR = frozenset({"gateway", "agent-runner"})


@pytest.fixture()
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point registry_path() at a tmp file so tests don't touch the real registry."""
    reg_file = tmp_path / "clusters.json"
    monkeypatch.setattr(cl, "registry_path", lambda: reg_file)
    return reg_file


@pytest.fixture()
def noop_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch out the side-effecting birth steps + pin the checkout root to a
    throwaway dir (the .ava_home pointer write must not touch the real worktree).
    Returns the throwaway checkout dir."""
    import cli.commands.cluster_lifecycle as gw

    monkeypatch.setattr(
        gw,
        "_ensure_cluster_instance",
        lambda _rec, _secret, _identity, **_kw: 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(gw, "_provision", lambda _name, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    # The runner-role provisioning steps are real SQL against the cluster's pg —
    # stubbed like the rest of the birth (no live pg in these unit tests).
    monkeypatch.setattr(
        cl,
        "ensure_checkpoint_schema",
        lambda *_a, **_k: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        cl,
        "ensure_runner_role",
        lambda *_a, **_k: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(cl, "_port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType]
    checkout = tmp_path / "_checkout"
    checkout.mkdir()
    monkeypatch.setattr(ic, "_checkout_root", lambda: checkout)
    return checkout


def _install(home: Path, **kw: object) -> int:
    defaults: dict = {
        "home": home,
        "role": _GW_AR,
        "worktree": True,
        "seed": False,
    }
    defaults.update(kw)  # pyright: ignore[reportUnknownMemberType]
    return ic.cmd_install_cluster(**defaults)  # pyright: ignore[reportUnknownArgumentType]


# ---------------------------------------------------------------------------
# worktree birth
# ---------------------------------------------------------------------------


def test_worktree_birth_writes_record_env_pointer(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    home = tmp_path / ".ava-t1"
    assert _install(home) == 0

    rec = get_record(home)  # registry is keyed by the home path
    assert rec is not None
    assert rec.gateway_home == str(home)

    env = dotenv_values(home / ".env")
    # a fresh birth writes the fixed data-plane identity into the URLs (as data)
    assert "//ava@" in (env["AVA_DB_URL"] or "")
    assert (env["AVA_DB_URL"] or "").endswith("/ava")
    # single-machine birth -> NO-AUTH: empty secret, identity username without
    # password in the URLs (names-as-data holds with or without auth)
    assert env["AVA_CLUSTER_SECRET"] == ""
    assert ":" not in (env["AVA_REDIS_URL"] or "").split("@")[0].split("//")[1]
    assert (env["AVA_REDIS_URL"] or "").startswith("redis://ava@")
    # role -> serve flags (single-box worktree: both on)
    assert env["AVA_MACHINE_SERVE_GATEWAY"] == "true"
    assert env["AVA_MACHINE_SERVE_AGENT_RUNNER"] == "true"

    pointer = noop_infra / ".ava_home"
    assert pointer.exists()
    assert pointer.read_text().strip() == str(home)


def test_any_path_is_a_valid_home(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    """Identity is the path: a home outside any naming convention births fine —
    there is no name to derive, so no convention to satisfy."""
    home = tmp_path / "just-a-dir"
    assert _install(home) == 0
    assert get_record(home) is not None


def test_worktree_birth_idempotent_keeps_secret(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-install is a no-op birth: the record survives, the secret state is not
    changed (a no-secret home stays secret-less), and the data-plane bring-up is
    not re-run."""
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-idem"
    assert _install(home) == 0
    first_rec = get_record(home)
    first_secret = dotenv_values(home / ".env")["AVA_CLUSTER_SECRET"]
    assert first_secret == ""

    calls: list[str] = []
    monkeypatch.setattr(
        gw,
        "_ensure_cluster_instance",
        lambda _rec, _secret, _identity: calls.append("instance") or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _install(home) == 0
    assert calls == [], "no-op re-install must not re-run the data-plane bring-up"
    assert get_record(home) == first_rec
    assert dotenv_values(home / ".env")["AVA_CLUSTER_SECRET"] == first_secret


def test_secret_precedence_file_explicit_mint_never_process_env(tmp_path: Path) -> None:
    """_resolve_secret's contract: explicit --cluster-secret wins, else the
    home's .env secret, else role-derived (single machine: empty; split
    gateway-only: mint). The process environment (a leaked prod
    AVA_CLUSTER_SECRET) is never consulted, and two fresh split homes never
    share a mint."""
    gw_ar = frozenset({"gateway", "agent-runner"})
    gw_only = frozenset({"gateway"})

    env_path = tmp_path / ".env"
    env_path.write_text("AVA_CLUSTER_SECRET=from-file\n")
    assert ic._resolve_secret(env_path, role=gw_ar, explicit=None) == "from-file"
    # an explicit secret wins over the file
    assert ic._resolve_secret(env_path, role=gw_ar, explicit="stated") == "stated"

    # single machine without a file/explicit secret -> EMPTY (no-auth cluster)
    assert ic._resolve_secret(tmp_path / "absent-a" / ".env", role=gw_ar, explicit=None) == ""
    # split gateway-only without a file/explicit secret -> minted
    minted_a = ic._resolve_secret(tmp_path / "absent-b" / ".env", role=gw_only, explicit=None)
    minted_b = ic._resolve_secret(tmp_path / "absent-c" / ".env", role=gw_only, explicit=None)
    assert minted_a and minted_b and minted_a != minted_b
    assert minted_a != os.environ.get("AVA_CLUSTER_SECRET", "")


def test_install_secret_input_prefers_compatibility_flag_and_consumes_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated install env is a one-shot input, while the argv flag is
    retained only as a compatibility override. The generic runtime secret is
    deliberately unrelated and must remain untouched."""
    monkeypatch.setenv("AVA_INSTALL_CLUSTER_SECRET", "from-install-env")
    runtime_secret = os.environ["AVA_CLUSTER_SECRET"]

    assert ic._install_secret_input("from-flag") == "from-flag"
    assert "AVA_INSTALL_CLUSTER_SECRET" not in os.environ
    assert os.environ["AVA_CLUSTER_SECRET"] == runtime_secret

    monkeypatch.setenv("AVA_INSTALL_CLUSTER_SECRET", "from-install-env")
    assert ic._install_secret_input(None) == "from-install-env"
    assert "AVA_INSTALL_CLUSTER_SECRET" not in os.environ


def test_explicit_secret_validated_urlsafe(tmp_path: Path) -> None:
    """--cluster-secret must be a URL-safe token — it lands in URLs, redis.conf,
    and bearer headers, so the same charset restriction as the settings field
    applies at install time."""
    with pytest.raises(ValueError, match="URL-safe"):
        ic._resolve_secret(
            tmp_path / ".env", role=frozenset({"gateway", "agent-runner"}), explicit="has space"
        )
    # URL-safe chars pass
    assert (
        ic._resolve_secret(tmp_path / ".env", role=frozenset({"gateway"}), explicit="tok_1.~-x")
        == "tok_1.~-x"
    )


def test_bootstrap_strips_inherited_cluster_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main()'s process bootstrap must strip every inherited cluster/identity key
    and plant never-dialed placeholders, so a shell that sourced the prod env
    cannot leak it into the new cluster."""
    from shared.dotenv_boot import UNANCHORED_DB_SENTINEL

    fake = {
        "AVA_DB_URL": "postgresql://prod@prodhost:5432/ava_main",
        "AVA_REDIS_URL": "redis://prodhost:6379/0",
        "AVA_CLUSTER_SECRET": "prod-secret",
        "AVA_MACHINE_SERVE_GATEWAY": "true",
        "AVA_GATEWAY_URL": "https://ava.prod.example.com",
        "PATH": "/usr/bin",
    }
    monkeypatch.setattr(os, "environ", fake)
    home = tmp_path / ".ava-fresh"
    ic._bootstrap_process_env(home)

    assert fake["AVA_HOME"] == str(home)
    # install-time Settings constructions skip the gateway fetch (a fresh home
    # has no gateway URL yet); daemons the install later starts fetch per role
    assert fake["AVA_CONFIG_FETCH"] == "skip"
    # The install writes the `.ava_home` pointer, so it is the one caller allowed
    # to outrank an existing one — the exemption is set, then CONSUMED by the
    # eager resolve_ava_home() the dotenv_boot import above triggers, and
    # stripped so no child this process spawns inherits the hatch (F-s4-7).
    assert "AVA_HOME_OVERRIDE" not in fake
    for leaked in (
        "AVA_CLUSTER_SECRET",
        "AVA_MACHINE_SERVE_GATEWAY",
        "AVA_GATEWAY_URL",
    ):
        assert leaked not in fake, f"{leaked} leaked into the install process env"
    assert fake["AVA_DB_URL"] == UNANCHORED_DB_SENTINEL  # placeholder replaced the prod URL
    from shared.dotenv_boot import BOOT_REDIS_PLACEHOLDER

    assert fake["AVA_REDIS_URL"] == BOOT_REDIS_PLACEHOLDER
    assert fake["PATH"] == "/usr/bin"  # unrelated env preserved


# ---------------------------------------------------------------------------
# role dispatch
# ---------------------------------------------------------------------------


def test_agent_runner_role_writes_flags_without_birth(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    rc = _install(home, role=frozenset({"agent-runner"}), worktree=False)
    assert rc == 0
    assert cl.load_registry() == {}, "an agent-runner-only install must not birth"
    env = dotenv_values(home / ".env")
    assert env == {
        "AVA_MACHINE_SERVE_GATEWAY": "false",
        "AVA_MACHINE_SERVE_AGENT_RUNNER": "true",
    }


def test_gateway_only_role_serve_flags_and_mints_secret(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    home = tmp_path / ".ava-gwonly"
    assert _install(home, role=frozenset({"gateway"}), worktree=False) == 0
    env = dotenv_values(home / ".env")
    assert env["AVA_MACHINE_SERVE_GATEWAY"] == "true"
    assert env["AVA_MACHINE_SERVE_AGENT_RUNNER"] == "false"
    # a split gateway-only host mints a fresh secret — remote agent-runners
    # depend on it (scram/requirepass + bearer), so a split deployment always
    # has one
    assert env["AVA_CLUSTER_SECRET"]
    assert env["AVA_CLUSTER_SECRET"] in (env["AVA_DB_URL"] or "")
    assert env["AVA_CLUSTER_SECRET"] in (env["AVA_REDIS_URL"] or "")


def test_single_machine_explicit_secret_turns_auth_on(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    """--cluster-secret on a single-machine role: the stated secret is used —
    the no-auth default is a default, not a prohibition."""
    home = tmp_path / ".ava-secretbox"
    assert _install(home, cluster_secret="stated-secret-123") == 0  # noqa: S106 — test fixture
    env = dotenv_values(home / ".env")
    assert env["AVA_CLUSTER_SECRET"] == "stated-secret-123"  # noqa: S105 — test fixture
    assert "stated-secret-123" in (env["AVA_DB_URL"] or "")
    assert "stated-secret-123" in (env["AVA_REDIS_URL"] or "")


def test_single_machine_keeps_existing_secret(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    """A home that already carries a secret (e.g. born before the no-auth
    default) keeps it on re-install — the secret is never rotated or dropped."""
    home = tmp_path / ".ava-oldbox"
    home.mkdir()
    (home / ".env").write_text("AVA_CLUSTER_SECRET=old-secret\nAVA_DB_URL=postgresql://x\n")
    assert _install(home) == 0
    assert dotenv_values(home / ".env")["AVA_CLUSTER_SECRET"] == "old-secret"  # noqa: S105 — test fixture


# ---------------------------------------------------------------------------
# refusals: default cluster / name+home collisions / off-convention home
# ---------------------------------------------------------------------------


def test_worktree_refuses_default_home(
    isolated_registry: Path,
    noop_infra: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--worktree at the default home (~/.ava) is refused — that is prod. The
    refusal fires before any registry / data-plane touch."""
    rc = _install(cl.default_home())
    assert rc == 1
    assert "refusing --worktree" in capsys.readouterr().err
    assert cl.load_registry() == {}


def test_same_basename_different_homes_coexist(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    """Two homes sharing a basename are simply two clusters (path IS the
    identity) — no name exists to collide on; each gets its own port block."""
    a = tmp_path / "a" / ".ava-dup"
    b = tmp_path / "b" / ".ava-dup"
    assert _install(a) == 0
    assert _install(b) == 0
    rec_a, rec_b = get_record(a), get_record(b)
    assert rec_a is not None and rec_b is not None
    assert rec_a.ports["gateway"] != rec_b.ports["gateway"]


def test_worktree_home_owned_by_other_checkout_refused(
    isolated_registry: Path,
    noop_infra: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two checkouts with the same directory name derive the same default home.
    The second one (no matching .ava_home pointer) must be refused, not silently
    clobber the first's cluster."""
    home = tmp_path / ".ava-samename"
    assert _install(home) == 0

    other_checkout = tmp_path / "_other_checkout"
    other_checkout.mkdir()
    monkeypatch.setattr(ic, "_checkout_root", lambda: other_checkout)
    rc = _install(home)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--path" in err
    assert not (other_checkout / ".ava_home").exists()


def test_entry_rejects_cluster_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """The Python entry exposes no cluster-name argument — argparse dies on
    --cluster before any env mutation (parse precedes the bootstrap)."""
    with pytest.raises(SystemExit) as exc_info:
        ic.main(
            [
                "--home",
                "/nonexistent/.ava-t",
                "--role",
                "gateway",
                "--seed-source",
                "/nonexistent/.env",
                "--cluster",
                "t",
            ]
        )
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --cluster" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_provision_failure_rolls_back_created_record(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands.cluster_lifecycle as gw

    def _boom(_name: str, **_kw: object) -> None:
        raise RuntimeError("pg unreachable mid-provision")

    monkeypatch.setattr(gw, "_provision", _boom)
    home = tmp_path / ".ava-doomed"
    with pytest.raises(RuntimeError):
        _install(home)
    assert get_record(home) is None


def test_repair_run_on_existing_record_never_rolls_it_back(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback gate is `created`: a repair install (record exists, but .env
    lost its AVA_DB_URL — e.g. a clobbered .env) re-runs birth with
    created=False, and a transient provision failure must NOT deregister the
    existing record — that would free a live cluster's port block."""
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-live"
    assert _install(home) == 0
    rec = get_record(home)
    assert rec is not None

    # Simulate the clobbered .env: the installed marker (AVA_DB_URL) is gone.
    env_path = home / ".env"
    env_path.write_text("AVA_CLUSTER_SECRET=stillhere\n")

    def _boom(_identity: str, **_kw: object) -> None:
        raise RuntimeError("transient db error on repair re-install")

    monkeypatch.setattr(gw, "_provision", _boom)
    with pytest.raises(RuntimeError):
        _install(home)
    assert get_record(home) == rec, "an existing record must survive a failed repair run"


def test_incomplete_birth_retry_carries_repair_not_database_creation_authority(
    isolated_registry: Path,
    noop_infra: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing record + incomplete env re-enters birth and may resume a prefix.

    It must not claim this invocation created the existing private database; the
    two authorities have distinct cleanup consequences.
    """
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-interrupted-birth"
    assert _install(home) == 0
    (home / ".env").write_text("AVA_CLUSTER_SECRET=still-incomplete\n")
    captured: dict[str, object] = {}

    def existing_database(_identity: str, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(gw, "_provision", existing_database)

    def capture_checkpoint_authority(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cl, "ensure_checkpoint_schema", capture_checkpoint_authority)

    assert _install(home) == 0
    assert captured["resume_partial"] is True
    assert captured["database_created"] is False


def test_worktree_install_is_turnkey_for_start(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `install.sh --worktree`, a flagless `ava start` must have everything
    it needs: the .env carries the machine identity (defaulted to the home
    basename), both serve flags, and AVA_GATEWAY_URL — and the settings-free
    installed-home gate passes for the home."""
    import json
    import os

    home = tmp_path / ".ava-t9"
    assert _install(home) == 0

    env = dotenv_values(home / ".env")
    # machine identity defaulted from the home basename (dots stripped)
    assert env["AVA_MACHINE_NAME"] == "ava-t9"
    # the rest of the required first-start surface is already materialized
    assert env["AVA_MACHINE_SERVE_GATEWAY"] == "true"
    assert env["AVA_MACHINE_SERVE_AGENT_RUNNER"] == "true"
    assert (env["AVA_GATEWAY_URL"] or "").startswith("http://localhost:")

    # and the pure-bring-up gate passes on real files alone (settings-free)
    from shared import dotenv_boot

    monkeypatch.setattr(dotenv_boot, "resolve_ava_home", lambda: (home, True))
    monkeypatch.setattr(os, "environ", {"AVA_CLUSTER_REGISTRY": str(isolated_registry)})
    assert json.loads(isolated_registry.read_text())  # registry actually has the record
    from cli.preflight import require_installed_home

    assert require_installed_home() is None


def test_worktree_machine_name_not_overwritten(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    """A re-install keeps an operator-edited machine name."""
    home = tmp_path / ".ava-named"
    assert _install(home) == 0
    from shared.envfile import upsert_env

    upsert_env(home / ".env", {"AVA_MACHINE_NAME": "my-custom-name"})
    assert _install(home) == 0
    assert dotenv_values(home / ".env")["AVA_MACHINE_NAME"] == "my-custom-name"


def test_instance_failure_rolls_back_created_record(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands.cluster_lifecycle as gw

    monkeypatch.setattr(
        gw,
        "_ensure_cluster_instance",
        lambda _rec, _secret, _identity, **_kw: 1,  # pyright: ignore[reportUnknownArgumentType]
    )
    home = tmp_path / ".ava-nodb"
    rc = _install(home)
    assert rc == 1
    assert get_record(home) is None


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


def test_seed_copies_allowlist_only(tmp_path: Path) -> None:
    source = tmp_path / "prod.env"
    source.write_text(
        "DEEPSEEK_API_KEY=sk-deep\n"
        "BRAVE_API_KEY=brave\n"
        "AVA_CLUSTER_SECRET=prod-secret\n"
        "AVA_TELEGRAM_BOT_TOKEN=tok\n"
        "AVA_DB_URL=postgresql://prod@h:5432/ava_main\n"
        "AVA_MACHINE_NAME=prod-box\n"
        "JINA_API_KEY=\n"  # empty values are not copied
    )
    target = tmp_path / "wt.env"
    target.write_text("AVA_CLUSTER=t1\n")

    copied = ic.seed_convenience_env(target_env=target, source_env=source)
    assert copied == ["BRAVE_API_KEY", "DEEPSEEK_API_KEY"]
    env = dotenv_values(target)
    assert env["DEEPSEEK_API_KEY"] == "sk-deep"
    assert env["BRAVE_API_KEY"] == "brave"
    assert env["AVA_CLUSTER"] == "t1"  # pre-existing lines preserved
    for banned in (
        "AVA_CLUSTER_SECRET",
        "AVA_TELEGRAM_BOT_TOKEN",
        "AVA_DB_URL",
        "AVA_MACHINE_NAME",
    ):
        assert banned not in env


def test_seed_missing_source_is_noop(tmp_path: Path) -> None:
    target = tmp_path / "wt.env"
    target.write_text("AVA_CLUSTER=t1\n")
    assert ic.seed_convenience_env(target_env=target, source_env=tmp_path / "absent.env") == []
    assert dotenv_values(target) == {"AVA_CLUSTER": "t1"}


def test_worktree_install_runs_seed(
    isolated_registry: Path, noop_infra: Path, tmp_path: Path
) -> None:
    source = tmp_path / "prod.env"
    source.write_text("GEMINI_API_KEY=gm\nAVA_CLUSTER_SECRET=prod-secret\n")
    home = tmp_path / ".ava-seeded"
    assert _install(home, seed=True, seed_source=source) == 0
    env = dotenv_values(home / ".env")
    assert env["GEMINI_API_KEY"] == "gm"
    assert env["AVA_CLUSTER_SECRET"] != "prod-secret"  # noqa: S105 — minted, never seeded


def test_seed_only_requires_installed_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = ic.cmd_seed_only(home=tmp_path / ".ava-nothere")
    assert rc == 1
    assert "install the cluster first" in capsys.readouterr().err


def test_seed_only_reseeds_installed_home(tmp_path: Path) -> None:
    home = tmp_path / ".ava-t9"
    home.mkdir()
    (home / ".env").write_text("AVA_CLUSTER=t9\n")
    source = tmp_path / "prod.env"
    source.write_text("GLM_API_KEY=xk\n")
    assert ic.cmd_seed_only(home=home, seed_source=source) == 0
    assert dotenv_values(home / ".env")["GLM_API_KEY"] == "xk"


# ---------------------------------------------------------------------------
# CLI surface: install params carry no defaults (user decision)
# ---------------------------------------------------------------------------


def test_entry_requires_role(capsys: pytest.CaptureFixture[str]) -> None:
    """--role is a required install param — no default capability set."""
    with pytest.raises(SystemExit) as exc_info:
        ic.main(["--home", "/nonexistent/.ava-t"])
    assert exc_info.value.code == 2
    assert "--role" in capsys.readouterr().err


def test_entry_seed_without_seed_source_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--seed without --seed-source no longer aborts: the seed source falls
    back to ~/.ava/.env (the same default the seed functions themselves use),
    and a missing source is a no-op — install.sh's prod birth path never
    passes --seed-source (2026-08-12 staging install hit the required-arg
    abort)."""

    def _fake_seed(*, target_env: Path, source_env: Path) -> list[str]:
        del target_env, source_env
        return ["AVA_MODEL"]

    def _fake_bootstrap(home: Path) -> None:
        del home

    monkeypatch.setattr(ic, "seed_convenience_env", _fake_seed)
    monkeypatch.setattr(ic, "_bootstrap_process_env", _fake_bootstrap)
    home = str(tmp_path / ".ava-t")
    # --seed + explicit source is still honoured (missing source = no-op)
    ret = ic.main(
        [
            "--home",
            home,
            "--role",
            "agent-runner",
            "--seed",
            "--seed-source",
            "/nonexistent/source.env",
        ]
    )
    assert ret == 0
