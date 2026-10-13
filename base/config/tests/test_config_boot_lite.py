"""Runtime battery for the boot-lite config chain (#3621).

Every test that must observe the DEFAULT boot (a lite process) runs in a
subprocess: this pytest process boots eager (`tests/fixtures/env_bootstrap.py`). The child
env is scrubbed of the surrounding process's `AVA_*` projection so each child
sees the CI-clean environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHILD = str(Path(__file__).with_name("_boot_lite_child.py"))

# The real merged pin map of a `deepseek-v4-flash` exec child (task #3621
# BLK-1): what `AVA_AGENT_CONFIG_OVERLAY` carries in production.
_PINMAP: dict[str, object] = {
    "agent_communication_style": "off",
    "claude_thinking_budget_tokens": None,
    "eval_isolation": False,
    "eval_network_allowlist": [],
    "llm_model": "deepseek-v4-flash",
    "reasoning_effort": None,
    "sdk_disable": [],
    "skills_to_expand_at_start": [],
    "skills_to_inject_into_system_prompt": ["*"],
    "system_prompt_extra": [],
}

# Names a boot-lite process may have imported under `base.config`.
_LITE_CONFIG_MODULES = {
    "base.config",
    "base.config._lite",
    "base.config.profiles",
    "base.config.service_read",
    "base.host.env.config_lite_table",
    "base.config.config_registry",
}


@pytest.fixture(autouse=True)
def _isolated_boot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default child boots a local source; explicit home cases still override it."""
    home = tmp_path / "boot-home"
    home.mkdir()
    (home / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_DB_URL=postgresql://test:test@127.0.0.1:1/test\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AVA_HOME", str(home))


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.pop("VIRTUAL_ENV", None)
    # Keep pytest's isolated home; cleaning config keys must not select a live unit.
    env["AVA_HOME"] = os.environ["AVA_HOME"]
    env.update(overrides)
    return env


def _spawn(
    *argv: str, env: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", *argv],
        check=False,
        cwd=_REPO_ROOT,
        env=_clean_env(**(env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_default_boot_is_lite_and_pydantic_settings_free() -> None:
    proc = _spawn(_CHILD, "default_boot_is_lite_and_pydantic_settings_free")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("BOOT lite 0 False"), proc.stdout
    assert set(proc.stdout.partition("[")[2].rstrip("]\n").replace("'", "").split(", ")) <= (
        _LITE_CONFIG_MODULES
    )


@pytest.mark.parametrize("existing_images", [False, True], ids=["first-install", "existing-images"])
def test_exec_child_closure_stays_lite_with_real_pin_map(
    tmp_path: Path, existing_images: bool
) -> None:
    """The §5 canary: a crafted exec child booting with the real pin map must
    not upgrade, must not import `pydantic_settings`, and must not import any
    heavy `base.config` submodule. This is the regression gate for BLK-2's
    two direct-import edges (`agent.db` -> base.db.connections, `agent.graph` ->
    exec._crop)."""
    if existing_images:
        # A separate process writes the plugin images, so this test imports nothing from packages.
        updated = _spawn(
            "-c",
            "from base.packages.plugin_config_admin import update_all_disk_images as update\n"
            "result = update()\n"
            "assert not any(entry.status == 'error' for entry in result.entries), result\n",
        )
        assert updated.returncode == 0, updated.stderr
        assert (Path(os.environ["AVA_HOME"]) / "configs" / "ava_fleet" / "config.json").is_file()
    request = tmp_path / "req.json"
    result = tmp_path / "res.json"
    code = (
        "import json, sys\n"
        "import base.config as c\n"
        "from ava.sdk_surface.settings import agent_setting\n"
        f"pins = {{name: agent_setting(name) for name in {tuple(_PINMAP)!r}}}\n"
        "st = c._boot_state()\n"
        "cfgmods = sorted(m for m in sys.modules if m.startswith('base.config'))\n"
        "print('CANARY ' + json.dumps({'mode': st['mode'], 'upgrades': st['upgrades'],\n"
        "  'pins': pins, 'pydantic_settings': 'pydantic_settings' in sys.modules,\n"
        "  'shared_config_mods': cfgmods}, sort_keys=True))\n"
    )
    request.write_text(
        json.dumps(
            {
                "v": 1,
                "code": code,
                "agent_id": None,
                "context": {
                    "identity": {"agent_id": None, "owns_loop": True, "actor": None},
                    "gateway_url": "http://gateway.invalid:8000",
                },
                "timeout_s": 30.0,
            }
        ),
        encoding="utf-8",
    )
    proc = _spawn(
        "-m",
        "agent.execution.child",
        env={
            "AVA_PROCESS_PROFILE": "agent",
            "AVA_AGENT_CONFIG_OVERLAY": json.dumps(_PINMAP),
            "AVA_EXEC_REQUEST_FILE": str(request),
            "AVA_EXEC_RESULT_FILE": str(result),
        },
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["kind"] == "done", payload
    line = next(line for line in proc.stdout.splitlines() if line.startswith("CANARY "))
    canary = json.loads(line.removeprefix("CANARY "))
    assert canary["mode"] == "lite"
    assert canary["upgrades"] == 0
    assert canary["pins"] == _PINMAP
    assert canary["pydantic_settings"] is False
    assert set(canary["shared_config_mods"]) <= _LITE_CONFIG_MODULES


def test_eager_escape_boots_full_at_import() -> None:
    proc = _spawn(
        _CHILD,
        "eager_escape_boots_full_at_import",
        env={"AVA_CONFIG_BOOT": "eager"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("EAGER full Settings "), proc.stdout


def test_config_fetch_skip_wins_over_eager() -> None:
    proc = _spawn(
        _CHILD,
        "config_fetch_skip_wins_over_eager",
        env={"AVA_CONFIG_BOOT": "eager", "AVA_CONFIG_FETCH": "skip"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("SKIPWIN lite False"), proc.stdout


def test_dotenv_boot_mode_is_not_honored(tmp_path: Path) -> None:
    """`AVA_CONFIG_BOOT` is read from the PROCESS environment only: a value
    living in the unit `.env` is loaded later (inside prepare) and cannot flip
    this boot. The design documents this; the test pins it."""
    (tmp_path / ".env").write_text(
        "AVA_CONFIG_BOOT=eager\n"
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_DB_URL=postgresql://ava:pw@127.0.0.1:5432/ava\n"
        "AVA_REDIS_URL=redis://127.0.0.1:6380/0\n",
        encoding="utf-8",
    )
    proc = _spawn(
        _CHILD,
        "dotenv_boot_mode_is_not_honored",
        env={"AVA_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DOTENV lite eager"), proc.stdout


def test_required_fields_fail_fast_for_a_local_source(tmp_path: Path) -> None:
    """W1: the local-source branch keeps the eager boot's fail-fast for
    no-default fields; the unit's own `.env` must carry the serve flag (the env
    authority pass drops inherited machine-identity keys)."""
    (tmp_path / ".env").write_text("AVA_MACHINE_SERVE_GATEWAY=true\n", encoding="utf-8")
    proc = _spawn(_CHILD, "import_config", env={"AVA_HOME": str(tmp_path)})
    assert proc.returncode != 0
    assert "required config missing" in proc.stderr and "AVA_DB_URL" in proc.stderr, proc.stderr


def test_overlay_writes_stay_pending_and_never_upgrade() -> None:
    proc = _spawn(_CHILD, "overlay_writes_stay_pending_and_never_upgrade")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("PENDING overlay-model 123.0 0 "), proc.stdout


def test_overlay_replays_in_order_on_upgrade() -> None:
    proc = _spawn(_CHILD, "overlay_replays_in_order_on_upgrade")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("REPLAY replayed 7.5 full 1 0"), proc.stdout


def test_overlay_unknown_and_out_of_profile_keys_fail_fast() -> None:
    proc = _spawn(
        _CHILD,
        "overlay_unknown_and_out_of_profile_keys_fail_fast",
        env={"AVA_PROCESS_PROFILE": "agent"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("BADKEYS KeyError AttributeError 0"), proc.stdout


def test_lite_reads_see_environment_changes_live() -> None:
    """The documented lite invariant (W5-e): lite reads resolve `os.environ` at
    read time — unlike the eager construction's boot snapshot."""
    proc = _spawn(_CHILD, "lite_reads_see_environment_changes_live")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("LIVE ") and proc.stdout.rstrip().endswith(" 0"), proc.stdout
    assert "changed-later" in proc.stdout


def _home_with_env(tmp_path: Path, lines: list[str]) -> dict[str, str]:
    (tmp_path / ".env").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return {"AVA_HOME": str(tmp_path)}


def test_first_error_follows_construction_order(tmp_path: Path) -> None:
    """MAJ-1: with several bad fields, the first reported error is the same
    field in both boots — the boot-path validation loop walks the table in
    registry insertion order (= eager construction order), and an out-of-table
    defect surfaces through the same eager construction either mode reaches.

    Values are injected through the unit `.env` because the boot authority pass
    drops-or-forces cluster-scope aliases against it."""
    lite_home = tmp_path / "lite"
    lite_home.mkdir()
    lite_env = _home_with_env(
        lite_home,
        [
            "AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS=abc",  # agent domain, early
            "AVA_TIMEZONE=Bogus/Zone",  # general domain, later
        ],
    )
    lite = _spawn(_CHILD, "import_config", env=lite_env)
    eager = _spawn(
        _CHILD,
        "import_config",
        env={**lite_env, "AVA_CONFIG_BOOT": "eager"},
    )
    for proc in (lite, eager):
        assert proc.returncode != 0
        assert "AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS" in proc.stderr
        assert "AVA_TIMEZONE" not in proc.stderr

    eager_home = tmp_path / "eager"
    eager_home.mkdir()
    non_lite_env = _home_with_env(
        eager_home,
        [
            "AVA_EXEC_TIMEOUT_SECONDS=abc",  # sandbox domain, constructs early
            "AVA_TELEGRAM_POLL_TIMEOUT_SECONDS=abc",  # telegram domain, later
        ],
    )
    non_lite_eager = _spawn(
        _CHILD,
        "import_config",
        env={**non_lite_env, "AVA_CONFIG_BOOT": "eager"},
    )
    non_lite_lite = _spawn(_CHILD, "import_then_ensure_eager", env=non_lite_env)
    for proc in (non_lite_eager, non_lite_lite):
        assert proc.returncode != 0
        assert "AVA_EXEC_TIMEOUT_SECONDS" in proc.stderr
        assert "AVA_TELEGRAM_POLL_TIMEOUT_SECONDS" not in proc.stderr


def test_concurrent_first_touch_upgrades_once() -> None:
    """The upgrade is single-lock idempotent; readers that arrive while another
    thread's build is in flight wait for it (bounded) and still get the value,
    and every read after the join is correct."""
    proc = _spawn(_CHILD, "concurrent_first_touch_upgrades_once")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("CONC 1 0 8 True"), proc.stdout


def test_inflight_build_read_waits_then_serves_the_full_value() -> None:
    """A read that lands while another thread's eager build is in flight waits
    for the build instead of failing, and is served the full value once the
    build installs (task #4069: the window used to hard-fail the reader)."""
    proc = _spawn(_CHILD, "inflight_build_read_waits_then_serves_the_full_value")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("WAIT 1 True True True"), proc.stdout


def test_inflight_build_read_timeout_raises_retryable() -> None:
    """When the in-flight build overruns the bounded wait, the reader raises
    ConfigBuildWaitTimeoutError (clearly retryable) instead of hanging forever, and
    the in-flight build still completes for the thread that owns it."""
    proc = _spawn(_CHILD, "inflight_build_read_timeout_raises_retryable")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("TMOUT 1 True True True"), proc.stdout


def test_inflight_build_same_thread_read_does_not_self_wait() -> None:
    """The building thread must never wait on its own build: a read it makes
    mid-build keeps the documented window behavior (raise), completes at once,
    and the build itself still succeeds."""
    proc = _spawn(_CHILD, "inflight_build_same_thread_read_does_not_self_wait")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("SELF 1 True True"), proc.stdout


def test_inflight_build_get_field_waits_then_serves() -> None:
    """The `get_field` face takes the same bounded wait: a reflective read that
    lands while another thread's build is in flight waits for it and is served
    the full value (formalized from the task #4069 QA probe)."""
    proc = _spawn(_CHILD, "inflight_build_get_field_waits_then_serves")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("GETFL 1 True True True"), proc.stdout


def test_inflight_build_get_field_timeout_raises_retryable() -> None:
    """The `get_field` bounded wait expires into the same retryable error as the
    view faces: the reflective reader raises ConfigBuildWaitTimeoutError and a
    later retry serves the full value (formalized from the task #4069 QA
    probe)."""
    proc = _spawn(_CHILD, "inflight_build_get_field_timeout_raises_retryable")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("GETFLT 1 True True True True"), proc.stdout


def test_pre_install_build_death_revives_on_waiting_reader() -> None:
    """A build that dies before installing hands the waiting reader the revive
    path: the builder sees its real error, the waiter re-runs the build itself
    and is served, and exactly one successful install is counted (formalized
    from the task #4069 QA probe)."""
    proc = _spawn(_CHILD, "pre_install_build_death_revives_on_waiting_reader")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DEATH 1 True True True"), proc.stdout


def test_config_build_wait_timeout_is_exported() -> None:
    """The retryable exception is importable from the public facade (the catch
    shape callers may rely on)."""
    proc = _spawn(_CHILD, "config_build_wait_timeout_is_exported")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("EXPORT True True"), proc.stdout


def test_facade_settings_rebinds_to_the_singleton_after_upgrade() -> None:
    """The upgrade-time binding contract: pre-upgrade holders keep a working
    view; the facade name `settings` becomes the constructed singleton."""
    proc = _spawn(_CHILD, "facade_settings_rebinds_to_the_singleton_after_upgrade")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("REBIND _SettingsView Settings False True"), proc.stdout


def test_current_facade_resolves_after_upgrade() -> None:
    """The lite and eager config surfaces preserve Settings identity."""
    proc = _spawn(_CHILD, "current_facade_resolves_after_upgrade")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("FACADE True [] base.config True full"), proc.stdout


def test_forked_child_keeps_full_state() -> None:
    proc = _spawn(
        _CHILD,
        "forked_child_keeps_full_state",
        env={"AVA_CONFIG_BOOT": "eager"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("FORK 1"), proc.stdout


def test_bad_non_lite_field_does_not_block_lite_scripts(tmp_path: Path) -> None:
    """W5-f: a script that only reads lite surfaces keeps working even when an
    unrelated boot-path-external field holds a bad value; touching that field's
    surface upgrades and raises exactly as eager would."""
    (tmp_path / ".env").write_text("AVA_EXEC_TIMEOUT_SECONDS=not-a-number\n", encoding="utf-8")
    proc = _spawn(
        _CHILD,
        "bad_non_lite_field_does_not_block_lite_scripts",
        env={"AVA_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("SCRIPT AVA_MODEL "), proc.stdout
    assert proc.stdout.rstrip().endswith(" 0 yes"), proc.stdout


def test_field_faces_serve_while_another_thread_builds() -> None:
    """The settings-free faces must not race another thread's eager build: a
    `from base.config import _FIELDS` that lands while a build is in flight is
    served in place — without waiting and without upgrading (task #4964: the
    delivery_outbox keyed-send path used to get a spurious ImportError from
    the global `is_upgrading()` gate; these faces never depend on the
    upgrade)."""
    proc = _spawn(_CHILD, "field_faces_serve_while_another_thread_builds")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("FACES ok True 0 full"), proc.stdout


def test_module_attribute_read_waits_then_serves() -> None:
    """A module-attribute read (`getattr(base.config, 'Settings')`) takes the
    same bounded wait as the view and `get_field` faces (task #4069): it lands
    during another thread's in-flight build, waits for it, and is served the
    installed value — while the building thread's own re-entrant read keeps
    the documented window error instead of self-waiting (task #4964)."""
    proc = _spawn(_CHILD, "module_attribute_read_waits_then_serves")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ATTR 1 True True True True True"), proc.stdout
