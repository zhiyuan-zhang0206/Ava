"""Runtime battery for the boot-lite config chain (#3621).

Every test that must observe the DEFAULT boot (a lite process) runs in a
subprocess: this pytest process boots eager (`tests/conftest.py`). The child
env is scrubbed of the surrounding process's `AVA_*` projection so each child
sees the CI-clean environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

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

# Names a boot-lite process may have imported under `shared.config`.
_LITE_CONFIG_MODULES = {
    "shared.config",
    "shared.config._lite",
    "shared.config.profiles",
    "shared.config.turn_view",
    "shared.config_lite_table",
    "shared.config_registry",
}


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.pop("VIRTUAL_ENV", None)
    env.update(overrides)
    return env


def _spawn(
    code: str, *, env: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", "-c", code],
        check=False,
        cwd=_REPO_ROOT,
        env=_clean_env(**(env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_default_boot_is_lite_and_pydantic_settings_free() -> None:
    proc = _spawn(
        "import sys, shared.config as c\n"
        "st = c._boot_state()\n"
        "mods = sorted(m for m in sys.modules if m.startswith('shared.config'))\n"
        "print('BOOT', st['mode'], st['upgrades'], 'pydantic_settings' in sys.modules, mods)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("BOOT lite 0 False"), proc.stdout
    assert set(proc.stdout.partition("[")[2].rstrip("]\n").replace("'", "").split(", ")) <= (
        _LITE_CONFIG_MODULES
    )


def test_exec_child_closure_stays_lite_with_real_pin_map(tmp_path: Path) -> None:
    """The §5 canary: a crafted exec child booting with the real pin map must
    not upgrade, must not import `pydantic_settings`, and must not import any
    heavy `shared.config` submodule. This is the regression gate for BLK-2's
    two direct-import edges (`agent.db` -> db_connections, `agent.graph` ->
    _exec_crop)."""
    request = tmp_path / "req.json"
    result = tmp_path / "res.json"
    code = (
        "import json, sys\n"
        "import shared.config as c\n"
        "st = c._boot_state()\n"
        "cfgmods = sorted(m for m in sys.modules if m.startswith('shared.config'))\n"
        "print('CANARY ' + json.dumps({'mode': st['mode'], 'upgrades': st['upgrades'],\n"
        "  'pending': st['pending_count'], 'pydantic_settings': 'pydantic_settings' in sys.modules,\n"
        "  'shared_config_mods': cfgmods}, sort_keys=True))\n"
    )
    request.write_text(
        json.dumps({"v": 1, "code": code, "agent_id": None, "timeout_s": 30.0}),
        encoding="utf-8",
    )
    driver = (
        "import runpy, sys\n"
        "sys.argv = ['agent.exec_child']\n"
        "runpy.run_module('agent.exec_child', run_name='__main__')\n"
    )
    proc = _spawn(
        driver,
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
    assert canary["pending"] == len(_PINMAP)
    assert canary["pydantic_settings"] is False
    assert set(canary["shared_config_mods"]) <= _LITE_CONFIG_MODULES


def test_import_ava_stays_lite() -> None:
    proc = _spawn(
        "import sys, ava\n"
        "import shared.config as c\n"
        "st = c._boot_state()\n"
        "print('AVA', st['mode'], st['upgrades'], 'pydantic_settings' in sys.modules, "
        "'shared.config._base' in sys.modules)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("AVA lite 0 False False"), proc.stdout


def test_eager_escape_boots_full_at_import() -> None:
    proc = _spawn(
        "import shared.config as c\n"
        "print('EAGER', c._boot_state()['mode'], type(c.settings).__name__, "
        "c.settings.lm.llm_model)\n",
        env={"AVA_CONFIG_BOOT": "eager"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("EAGER full Settings "), proc.stdout


def test_config_fetch_skip_wins_over_eager() -> None:
    proc = _spawn(
        "import shared.config as c\n"
        "st = c._boot_state()\n"
        "print('SKIPWIN', st['mode'], st['prepared'])\n",
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
        "import os, shared.config as c\n"
        "print('DOTENV', c._boot_state()['mode'], os.environ.get('AVA_CONFIG_BOOT'))\n",
        env={"AVA_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DOTENV lite eager"), proc.stdout


def test_required_fields_fail_fast_for_a_local_source(tmp_path: Path) -> None:
    """W1: the local-source branch keeps the eager boot's fail-fast for
    no-default fields; the unit's own `.env` must carry the serve flag (the env
    authority pass drops inherited machine-identity keys)."""
    (tmp_path / ".env").write_text("AVA_MACHINE_SERVE_GATEWAY=true\n", encoding="utf-8")
    proc = _spawn("import shared.config\n", env={"AVA_HOME": str(tmp_path)})
    assert proc.returncode != 0
    assert "required config missing" in proc.stderr and "AVA_DB_URL" in proc.stderr, proc.stderr


def test_skip_mode_defers_prepare_and_plants_placeholders() -> None:
    proc = _spawn(
        "import os, shared.config as c\n"
        "prepared_before = c._boot_state()['prepared']\n"
        "value = c.settings.lm.llm_model\n"
        "print('DEFER', prepared_before, c._boot_state()['prepared'], value,\n"
        "      'unanchored' in os.environ.get('AVA_DB_URL', ''))\n",
        env={"AVA_CONFIG_FETCH": "skip"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DEFER False True "), proc.stdout
    assert proc.stdout.rstrip().endswith("True"), proc.stdout


def test_overlay_writes_stay_pending_and_never_upgrade() -> None:
    proc = _spawn(
        "import shared.config as c\n"
        "c.set_field('llm_model', 'overlay-model')\n"
        "c.set_field('exec_timeout_seconds', 123.0)\n"  # table-external key
        "st = c._boot_state()\n"
        "print('PENDING', c.settings.lm.llm_model, c.settings.sandbox.exec_timeout_seconds,\n"
        "      st['upgrades'], st['pending_count'])\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("PENDING overlay-model 123.0 0 "), proc.stdout


def test_overlay_replays_in_order_on_upgrade() -> None:
    proc = _spawn(
        "import shared.config as c\n"
        "c.set_field('llm_model', 'replayed')\n"
        "c.set_field('exec_timeout_seconds', 7.5)\n"
        "c.ensure_eager()\n"
        "st = c._boot_state()\n"
        "print('REPLAY', c.settings.lm.llm_model, c.settings.sandbox.exec_timeout_seconds,\n"
        "      st['mode'], st['upgrades'], st['pending_count'])\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("REPLAY replayed 7.5 full 1 0"), proc.stdout


def test_overlay_unknown_and_out_of_profile_keys_fail_fast() -> None:
    proc = _spawn(
        "import shared.config as c\n"
        "try:\n"
        "    c.set_field('definitely_not_a_field', 1)\n"
        "except KeyError:\n"
        "    unknown = 'KeyError'\n"
        "try:\n"
        "    c.set_field('telegram_bot_token', 'x')\n"  # telegram domain, excluded from agent
        "except AttributeError as exc:\n"
        "    excluded = 'AttributeError' if 'profile' in str(exc) else str(exc)\n"
        "print('BADKEYS', unknown, excluded, c._boot_state()['upgrades'])\n",
        env={"AVA_PROCESS_PROFILE": "agent"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("BADKEYS KeyError AttributeError 0"), proc.stdout


def test_lite_reads_see_environment_changes_live() -> None:
    """The documented lite invariant (W5-e): lite reads resolve `os.environ` at
    read time — unlike the eager construction's boot snapshot."""
    proc = _spawn(
        "import os, shared.config as c\n"
        "first = c.settings.lm.llm_model\n"
        "os.environ['AVA_MODEL'] = 'changed-later'\n"
        "second = c.settings.lm.llm_model\n"
        "print('LIVE', first, second, c._boot_state()['upgrades'])\n"
    )
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
    lite = _spawn("import shared.config\n", env=lite_env)
    eager = _spawn("import shared.config\n", env={**lite_env, "AVA_CONFIG_BOOT": "eager"})
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
        "import shared.config\n", env={**non_lite_env, "AVA_CONFIG_BOOT": "eager"}
    )
    non_lite_lite = _spawn("import shared.config as c\nc.ensure_eager()\n", env=non_lite_env)
    for proc in (non_lite_eager, non_lite_lite):
        assert proc.returncode != 0
        assert "AVA_EXEC_TIMEOUT_SECONDS" in proc.stderr
        assert "AVA_TELEGRAM_POLL_TIMEOUT_SECONDS" not in proc.stderr


def test_concurrent_first_touch_upgrades_once() -> None:
    """The upgrade is single-lock idempotent; readers that arrive while another
    thread's build is in flight wait for it (bounded) and still get the value,
    and every read after the join is correct."""
    proc = _spawn(
        "import threading\n"
        "import shared.config as c\n"
        "errors = []\n"
        "values = []\n"
        "def touch():\n"
        "    try:\n"
        "        values.append(c.settings.sandbox.exec_timeout_seconds)\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        "        errors.append(str(exc))\n"
        "threads = [threading.Thread(target=touch) for _ in range(8)]\n"
        "[t.start() for t in threads]\n"
        "[t.join() for t in threads]\n"
        "after = c.settings.sandbox.exec_timeout_seconds\n"
        "print('CONC', c._boot_state()['upgrades'], len(errors), len(values),\n"
        "      all(value == after for value in values))\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("CONC 1 0 8 True"), proc.stdout


def test_inflight_build_read_waits_then_serves_the_full_value() -> None:
    """A read that lands while another thread's eager build is in flight waits
    for the build instead of failing, and is served the full value once the
    build installs (task #4069: the window used to hard-fail the reader)."""
    proc = _spawn(
        "import threading\n"
        "import shared.config as c\n"
        "import shared.config._full as full\n"
        "real = full.build\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "def gated():\n"
        "    entered.set()\n"
        "    if not release.wait(30):\n"
        "        raise RuntimeError('gate not released')\n"
        "    return real()\n"
        "full.build = gated\n"
        "out = {}\n"
        "def read(tag):\n"
        "    try:\n"
        "        out[tag] = c.settings.web.web_jina_reader_base\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        "        out[tag] = f'ERR {type(exc).__name__}: {exc}'\n"
        "t1 = threading.Thread(target=read, args=('a',))\n"
        "t1.start()\n"
        "assert entered.wait(30), 'build never started'\n"
        "t2 = threading.Thread(target=read, args=('b',))\n"
        "t2.start()\n"
        "t2.join(0.5)\n"
        "held = t2.is_alive()\n"
        "release.set()\n"
        "t1.join(30)\n"
        "t2.join(30)\n"
        "same = out.get('a') == out.get('b')\n"
        "served = str(out.get('b', '')).startswith('https://')\n"
        "print('WAIT', c._boot_state()['upgrades'], held, same, served)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("WAIT 1 True True True"), proc.stdout


def test_inflight_build_read_timeout_raises_retryable() -> None:
    """When the in-flight build overruns the bounded wait, the reader raises
    ConfigBuildWaitTimeoutError (clearly retryable) instead of hanging forever, and
    the in-flight build still completes for the thread that owns it."""
    proc = _spawn(
        "import threading\n"
        "import shared.config as c\n"
        "import shared.config._lite as lite\n"
        "import shared.config._full as full\n"
        "lite._BUILD_WAIT_TIMEOUT_SECONDS = 0.3\n"
        "real = full.build\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "def gated():\n"
        "    entered.set()\n"
        "    if not release.wait(30):\n"
        "        raise RuntimeError('gate not released')\n"
        "    return real()\n"
        "full.build = gated\n"
        "out = {}\n"
        "def read(tag):\n"
        "    try:\n"
        "        out[tag] = c.settings.web.web_jina_reader_base\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        "        out[tag] = f'{type(exc).__name__}: {exc}'\n"
        "t1 = threading.Thread(target=read, args=('a',))\n"
        "t1.start()\n"
        "assert entered.wait(30), 'build never started'\n"
        "t2 = threading.Thread(target=read, args=('b',))\n"
        "t2.start()\n"
        "t2.join(5)\n"
        "release.set()\n"
        "t1.join(30)\n"
        "b = str(out.get('b', ''))\n"
        "print('TMOUT', c._boot_state()['upgrades'],\n"
        "      str(out.get('a', '')).startswith('https://'),\n"
        "      b.startswith('ConfigBuildWaitTimeoutError'), 'retry' in b)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("TMOUT 1 True True True"), proc.stdout


def test_inflight_build_same_thread_read_does_not_self_wait() -> None:
    """The building thread must never wait on its own build: a read it makes
    mid-build keeps the documented window behavior (raise), completes at once,
    and the build itself still succeeds."""
    proc = _spawn(
        "import threading\n"
        "import shared.config as c\n"
        "import shared.config._full as full\n"
        "real = full.build\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "inside = {}\n"
        "def gated():\n"
        "    entered.set()\n"
        "    try:\n"
        "        c.settings.web.web_jina_reader_base\n"
        "        inside['result'] = 'READ-OK'\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        "        inside['result'] = f'{type(exc).__name__}: {exc}'\n"
        "    if not release.wait(30):\n"
        "        raise RuntimeError('gate not released')\n"
        "    return real()\n"
        "full.build = gated\n"
        "t = threading.Thread(target=lambda: c.settings.sandbox.exec_timeout_seconds)\n"
        "t.start()\n"
        "assert entered.wait(30), 'build never started'\n"
        "release.set()\n"
        "t.join(30)\n"
        "result = str(inside.get('result', '?'))\n"
        "print('SELF', c._boot_state()['upgrades'],\n"
        "      result.startswith('AttributeError: config boot-lite:'), 'in flight' in result)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("SELF 1 True True"), proc.stdout


def test_config_build_wait_timeout_is_exported() -> None:
    """The retryable exception is importable from the public facade (the catch
    shape callers may rely on)."""
    proc = _spawn(
        "import shared.config as c\n"
        "from shared.config import ConfigBuildWaitTimeoutError\n"
        "print('EXPORT', c.ConfigBuildWaitTimeoutError is ConfigBuildWaitTimeoutError,\n"
        "      isinstance(ConfigBuildWaitTimeoutError('x'), AttributeError))\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("EXPORT True True"), proc.stdout


def test_facade_settings_rebinds_to_the_singleton_after_upgrade() -> None:
    """The upgrade-time binding contract: pre-upgrade holders keep a working
    view; the facade name `settings` becomes the constructed singleton."""
    proc = _spawn(
        "import shared.config as c\n"
        "view = c.settings\n"
        "before = c.settings.lm.llm_model\n"
        "c.ensure_eager()\n"
        "singleton = c.settings\n"
        "print('REBIND', type(view).__name__, type(singleton).__name__,\n"
        "      singleton is view, view.lm.llm_model == singleton.lm.llm_model == before)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("REBIND _SettingsView Settings False True"), proc.stdout


def test_legacy_names_resolve_after_upgrade() -> None:
    """E7 (extended, 6303 RECHECK R4): the legacy `shared.config` surface stays reachable.

    The six settings-free helpers (five re-exports plus the
    `refresh_data_plane_settings` call-time shim) and the two registry-backed
    field faces (`_FIELDS`, `FIELD_INFOS` — reachable without the upgrade,
    though their first access imports the registry stack) serve while still
    lite; every other legacy name — `_plant_lite_placeholders` included —
    resolves through the latch, upgrading once. No legacy name may raise
    AttributeError/ImportError, and the upgraded surface keeps
    `Settings.__module__` pinned plus pickle identity. Names deliberately not
    carried (zero consumers): the four internals `_SettingsProxy`,
    `_SettingsState`, `_settings_lock`, `_settings_state`; the private
    `_LITE_REDIS_URL` and `_config_registry`; the third-party re-exports
    `BaseModel`, `Field`, `RLock`."""
    proc = _spawn(
        "import pickle\n"
        "import shared.config as c\n"
        "lite_served = ['load_ava_env', 'config_source_is_local', 'should_fetch_from_gateway',\n"
        "              'CONFIG_FETCH_ENV', 'CONFIG_FETCH_SKIP', 'refresh_data_plane_settings',\n"
        "              '_FIELDS', 'FIELD_INFOS']\n"
        "served_ok = all(hasattr(c, n) for n in lite_served) and c._boot_state()['mode'] == 'lite'\n"
        "from shared.config import _full\n"
        "missing = [n for n in _full._facade_exports() if not hasattr(c, n)]\n"
        "legacy_ok = hasattr(c, '_plant_lite_placeholders')\n"
        "pinned = c.Settings.__module__\n"
        "same = pickle.loads(pickle.dumps(c.Settings)) is c.Settings\n"
        "print('LEGACY', served_ok, missing, legacy_ok, pinned, same, c._boot_state()['mode'])\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("LEGACY True [] True shared.config True full"), proc.stdout


def test_forked_child_keeps_full_state() -> None:
    proc = _spawn(
        "import os\n"
        "import shared.config as c\n"
        "c.settings.lm.llm_model\n"
        "r, w = os.pipe()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    ok = c._boot_state()['mode'] == 'full' and bool(c.settings.lm.llm_model)\n"
        "    os.write(w, b'1' if ok else b'0')\n"
        "    os._exit(0)\n"
        "os.close(w)\n"
        "out = os.read(r, 1)\n"
        "os.waitpid(pid, 0)\n"
        "print('FORK', out.decode())\n",
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
        "import shared.config as c\n"
        "alias = c.field_alias('llm_model')\n"
        "value = c.settings.lm.llm_model\n"
        "st = c._boot_state()\n"
        "try:\n"
        "    c.settings.sandbox.exec_timeout_seconds\n"
        "    raised = 'no'\n"
        "except ValueError:\n"
        "    raised = 'yes'\n"
        "print('SCRIPT', alias, value, st['upgrades'], raised)\n",
        env={"AVA_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("SCRIPT AVA_MODEL "), proc.stdout
    assert proc.stdout.rstrip().endswith(" 0 yes"), proc.stdout
