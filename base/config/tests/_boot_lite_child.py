"""Boot-lite config scenarios in a fresh interpreter; never imported by a production entry.

Run as ``python -B _boot_lite_child.py <scenario>`` with the environment prepared by the parent test;
each scenario prints one tagged line the parent asserts on."""

from __future__ import annotations

import sys
from typing import Any


def emit(*parts: object) -> None:
    sys.stdout.write(" ".join(str(part) for part in parts) + "\n")


def default_boot_is_lite_and_pydantic_settings_free() -> None:
    import sys

    import base.config as c

    st = c.boot_state()
    mods = sorted(m for m in sys.modules if m.startswith("base.config"))
    emit("BOOT", st["mode"], st["upgrades"], "pydantic_settings" in sys.modules, mods)


def eager_escape_boots_full_at_import() -> None:
    import base.config as c

    emit("EAGER", c.boot_state()["mode"], type(c.settings).__name__, c.settings.lm.llm_model)


def config_fetch_skip_wins_over_eager() -> None:
    import base.config as c

    st = c.boot_state()
    emit("SKIPWIN", st["mode"], st["prepared"])


def dotenv_boot_mode_is_not_honored() -> None:
    import os

    import base.config as c

    emit("DOTENV", c.boot_state()["mode"], os.environ.get("AVA_CONFIG_BOOT"))


def present(value: object) -> bool:
    return value is not None


def import_config() -> None:
    import base.config  # noqa: F401  # pyright: ignore[reportUnusedImport]


def overlay_writes_stay_pending_and_never_upgrade() -> None:
    import base.config as c

    c.set_field("llm_model", "overlay-model")
    c.set_field("exec_timeout_seconds", 123.0)
    st = c.boot_state()
    emit(
        "PENDING",
        c.settings.lm.llm_model,
        c.settings.sandbox.exec_timeout_seconds,
        st["upgrades"],
        st["pending_count"],
    )


def overlay_replays_in_order_on_upgrade() -> None:
    import base.config as c

    c.set_field("llm_model", "replayed")
    c.set_field("exec_timeout_seconds", 7.5)
    c.ensure_eager()
    st = c.boot_state()
    emit(
        "REPLAY",
        c.settings.lm.llm_model,
        c.settings.sandbox.exec_timeout_seconds,
        st["mode"],
        st["upgrades"],
        st["pending_count"],
    )


def overlay_unknown_and_out_of_profile_keys_fail_fast() -> None:
    import base.config as c

    unknown = excluded = "not raised"
    try:
        c.set_field("definitely_not_a_field", 1)
    except KeyError:
        unknown = "KeyError"
    try:
        c.set_field("telegram_bot_token", "x")
    except AttributeError as exc:
        excluded = "AttributeError" if "profile" in str(exc) else str(exc)
    emit("BADKEYS", unknown, excluded, c.boot_state()["upgrades"])


def lite_reads_see_environment_changes_live() -> None:
    import os

    import base.config as c

    first = c.settings.lm.llm_model
    os.environ["AVA_MODEL"] = "changed-later"
    second = c.settings.lm.llm_model
    emit("LIVE", first, second, c.boot_state()["upgrades"])


def import_then_ensure_eager() -> None:
    import base.config as c

    c.ensure_eager()


def concurrent_first_touch_upgrades_once() -> None:
    import threading

    import base.config as c

    errors: list[str] = []
    values: list[Any] = []

    def touch():
        try:
            values.append(c.settings.sandbox.exec_timeout_seconds)
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=touch) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    after = c.settings.sandbox.exec_timeout_seconds
    emit(
        "CONC",
        c.boot_state()["upgrades"],
        len(errors),
        len(values),
        all(value == after for value in values),
    )


def inflight_build_read_waits_then_serves_the_full_value() -> None:
    import threading

    import base.config as c
    import base.config._full as full

    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.settings.web.web_jina_reader_base
        except Exception as exc:
            out[tag] = f"ERR {type(exc).__name__}: {exc}"

    t1 = threading.Thread(target=read, args=("a",))
    t1.start()
    assert entered.wait(30), "build never started"
    t2 = threading.Thread(target=read, args=("b",))
    t2.start()
    t2.join(0.5)
    held = t2.is_alive()
    release.set()
    t1.join(30)
    t2.join(30)
    same = out.get("a") == out.get("b")
    served = str(out.get("b", "")).startswith("https://")
    emit("WAIT", c.boot_state()["upgrades"], held, same, served)


def inflight_build_read_timeout_raises_retryable() -> None:
    import threading

    import base.config as c
    import base.config._full as full
    import base.config._lite as lite

    lite.BUILD_WAIT_TIMEOUT_SECONDS = 0.3
    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.settings.web.web_jina_reader_base
        except Exception as exc:
            out[tag] = f"{type(exc).__name__}: {exc}"

    t1 = threading.Thread(target=read, args=("a",))
    t1.start()
    assert entered.wait(30), "build never started"
    t2 = threading.Thread(target=read, args=("b",))
    t2.start()
    t2.join(5)
    release.set()
    t1.join(30)
    b = str(out.get("b", ""))
    emit(
        "TMOUT",
        c.boot_state()["upgrades"],
        str(out.get("a", "")).startswith("https://"),
        b.startswith("ConfigBuildWaitTimeoutError"),
        "retry" in b,
    )


def inflight_build_same_thread_read_does_not_self_wait() -> None:
    import threading

    import base.config as c
    import base.config._full as full

    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()
    inside: dict[str, Any] = {}

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        try:
            _ = c.settings.web.web_jina_reader_base
            inside["result"] = "READ-OK"
        except Exception as exc:
            inside["result"] = f"{type(exc).__name__}: {exc}"
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    t = threading.Thread(target=lambda: c.settings.sandbox.exec_timeout_seconds)
    t.start()
    assert entered.wait(30), "build never started"
    release.set()
    t.join(30)
    result = str(inside.get("result", "?"))
    emit(
        "SELF",
        c.boot_state()["upgrades"],
        result.startswith("AttributeError: config boot-lite:"),
        "in flight" in result,
    )


def inflight_build_get_field_waits_then_serves() -> None:
    import threading

    import base.config as c
    import base.config._full as full

    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.get_field("web_jina_reader_base")
        except Exception as exc:
            out[tag] = f"ERR {type(exc).__name__}: {exc}"

    t1 = threading.Thread(target=read, args=("a",))
    t1.start()
    assert entered.wait(30), "build never started"
    t2 = threading.Thread(target=read, args=("b",))
    t2.start()
    t2.join(0.5)
    held = t2.is_alive()
    release.set()
    t1.join(30)
    t2.join(30)
    same = out.get("a") == out.get("b")
    served = str(out.get("b", "")).startswith("https://")
    emit("GETFL", c.boot_state()["upgrades"], held, same, served)


def inflight_build_get_field_timeout_raises_retryable() -> None:
    import threading

    import base.config as c
    import base.config._full as full
    import base.config._lite as lite

    lite.BUILD_WAIT_TIMEOUT_SECONDS = 0.3
    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.get_field("web_jina_reader_base")
        except Exception as exc:
            out[tag] = f"{type(exc).__name__}: {exc}"

    t1 = threading.Thread(target=read, args=("a",))
    t1.start()
    assert entered.wait(30), "build never started"
    t2 = threading.Thread(target=read, args=("b",))
    t2.start()
    t2.join(5)
    release.set()
    t1.join(30)
    b = str(out.get("b", ""))
    retry = c.get_field("web_jina_reader_base")
    emit(
        "GETFLT",
        c.boot_state()["upgrades"],
        str(out.get("a", "")).startswith("https://"),
        b.startswith("ConfigBuildWaitTimeoutError"),
        "retry" in b,
        str(retry).startswith("https://"),
    )


def pre_install_build_death_revives_on_waiting_reader() -> None:
    import threading

    import base.config as c
    import base.config._full as full

    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def gated(*, env_boot: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            if not release.wait(30):
                raise RuntimeError("gate not released")
            raise RuntimeError("first build died before install")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.settings.web.web_jina_reader_base
        except Exception as exc:
            out[tag] = f"{type(exc).__name__}: {exc}"

    t1 = threading.Thread(target=read, args=("a",))
    t1.start()
    assert entered.wait(30), "build never started"
    t2 = threading.Thread(target=read, args=("b",))
    t2.start()
    t2.join(0.5)
    held = t2.is_alive()
    release.set()
    t1.join(30)
    t2.join(30)
    emit(
        "DEATH",
        c.boot_state()["upgrades"],
        held,
        str(out.get("a", "")).startswith("RuntimeError:"),
        str(out.get("b", "")).startswith("https://"),
    )


def config_build_wait_timeout_is_exported() -> None:
    import base.config as c
    from base.config import ConfigBuildWaitTimeoutError

    emit(
        "EXPORT",
        c.ConfigBuildWaitTimeoutError is ConfigBuildWaitTimeoutError,
        isinstance(ConfigBuildWaitTimeoutError("x"), AttributeError),
    )


def facade_settings_rebinds_to_the_singleton_after_upgrade() -> None:
    import base.config as c

    view = c.settings
    before = c.settings.lm.llm_model
    c.ensure_eager()
    singleton = c.settings
    emit(
        "REBIND",
        type(view).__name__,
        type(singleton).__name__,
        singleton is view,
        view.lm.llm_model == singleton.lm.llm_model == before,
    )


def current_facade_resolves_after_upgrade() -> None:
    import pickle

    import base.config as c

    lite_served = [
        "load_ava_env",
        "config_source_is_local",
        "should_fetch_from_gateway",
        "CONFIG_FETCH_ENV",
        "CONFIG_FETCH_SKIP",
        "refresh_data_plane_settings",
        "_FIELDS",
        "FIELD_INFOS",
    ]
    served_ok = all(hasattr(c, n) for n in lite_served) and c.boot_state()["mode"] == "lite"
    from base.config import _full

    missing = [n for n in _full.facade_exports() if not hasattr(c, n)]
    pinned = c.Settings.__module__
    same = pickle.loads(pickle.dumps(c.Settings)) is c.Settings  # noqa: S301
    emit("FACADE", served_ok, missing, pinned, same, c.boot_state()["mode"])


def forked_child_keeps_full_state() -> None:
    import os

    import base.config as c

    _ = c.settings.lm.llm_model
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        ok = c.boot_state()["mode"] == "full" and bool(c.settings.lm.llm_model)
        os.write(w, b"1" if ok else b"0")
        os._exit(0)
    os.close(w)
    out = os.read(r, 1)
    os.waitpid(pid, 0)
    emit("FORK", out.decode())


def bad_non_lite_field_does_not_block_lite_scripts() -> None:
    import base.config as c

    alias = c.field_alias("llm_model")
    value = c.settings.lm.llm_model
    st = c.boot_state()
    try:
        _ = c.settings.sandbox.exec_timeout_seconds
        raised = "no"
    except ValueError:
        raised = "yes"
    emit("SCRIPT", alias, value, st["upgrades"], raised)


def field_faces_serve_while_another_thread_builds() -> None:
    import threading

    import base.config as c
    import base.config._full as full

    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.settings.web.web_jina_reader_base
        except Exception as exc:
            out[tag] = f"ERR {type(exc).__name__}: {exc}"

    t = threading.Thread(target=read, args=("build",))
    t.start()
    assert entered.wait(30), "build never started"
    try:
        from base.config import _FIELDS, CONFIG_UNCHANGED_SENTINEL, FIELD_INFOS, ConfigFieldMeta

        faces = (
            "ok"
            if (
                len(_FIELDS) > 0
                and len(FIELD_INFOS) > 0
                and present(CONFIG_UNCHANGED_SENTINEL)
                and present(ConfigFieldMeta)
            )
            else "empty"
        )
    except Exception as exc:
        faces = f"{type(exc).__name__}: {exc}"
    held = t.is_alive()
    upgrades = c.boot_state()["upgrades"]
    release.set()
    t.join(30)
    emit("FACES", faces, held, upgrades, c.boot_state()["mode"])


def module_attribute_read_waits_then_serves() -> None:
    import threading

    import base.config as c
    import base.config._full as full

    real: Any = full.build
    entered = threading.Event()
    release = threading.Event()
    inside: dict[str, Any] = {}

    def gated(*, env_boot: Any) -> Any:
        entered.set()
        try:
            _ = c.Settings
            inside["reentrant"] = "READ-OK"
        except Exception as exc:
            inside["reentrant"] = f"{type(exc).__name__}: {exc}"
        if not release.wait(30):
            raise RuntimeError("gate not released")
        return real(env_boot=env_boot)

    full.build = gated
    out: dict[str, Any] = {}

    def read(tag: str) -> None:
        try:
            out[tag] = c.settings.web.web_jina_reader_base
        except Exception as exc:
            out[tag] = f"ERR {type(exc).__name__}: {exc}"

    t1 = threading.Thread(target=read, args=("build",))
    t1.start()
    assert entered.wait(30), "build never started"

    def read_attr(tag: str) -> None:
        try:
            out[tag] = c.Settings
        except Exception as exc:
            out[tag] = f"ERR {type(exc).__name__}: {exc}"

    t2 = threading.Thread(target=read_attr, args=("waiter",))
    t2.start()
    t2.join(0.5)
    held = t2.is_alive()
    release.set()
    t1.join(30)
    t2.join(30)
    served = getattr(out.get("waiter"), "__name__", "") == "Settings"
    same = out.get("waiter") is c.Settings
    reentrant = str(inside.get("reentrant", "?"))
    emit(
        "ATTR",
        c.boot_state()["upgrades"],
        held,
        served,
        same,
        reentrant.startswith("AttributeError: config boot-lite:"),
        "in flight" in reentrant,
    )


SCENARIOS = {
    "default_boot_is_lite_and_pydantic_settings_free": default_boot_is_lite_and_pydantic_settings_free,
    "eager_escape_boots_full_at_import": eager_escape_boots_full_at_import,
    "config_fetch_skip_wins_over_eager": config_fetch_skip_wins_over_eager,
    "dotenv_boot_mode_is_not_honored": dotenv_boot_mode_is_not_honored,
    "import_config": import_config,
    "overlay_writes_stay_pending_and_never_upgrade": overlay_writes_stay_pending_and_never_upgrade,
    "overlay_replays_in_order_on_upgrade": overlay_replays_in_order_on_upgrade,
    "overlay_unknown_and_out_of_profile_keys_fail_fast": overlay_unknown_and_out_of_profile_keys_fail_fast,
    "lite_reads_see_environment_changes_live": lite_reads_see_environment_changes_live,
    "import_then_ensure_eager": import_then_ensure_eager,
    "concurrent_first_touch_upgrades_once": concurrent_first_touch_upgrades_once,
    "inflight_build_read_waits_then_serves_the_full_value": inflight_build_read_waits_then_serves_the_full_value,
    "inflight_build_read_timeout_raises_retryable": inflight_build_read_timeout_raises_retryable,
    "inflight_build_same_thread_read_does_not_self_wait": inflight_build_same_thread_read_does_not_self_wait,
    "inflight_build_get_field_waits_then_serves": inflight_build_get_field_waits_then_serves,
    "inflight_build_get_field_timeout_raises_retryable": inflight_build_get_field_timeout_raises_retryable,
    "pre_install_build_death_revives_on_waiting_reader": pre_install_build_death_revives_on_waiting_reader,
    "config_build_wait_timeout_is_exported": config_build_wait_timeout_is_exported,
    "facade_settings_rebinds_to_the_singleton_after_upgrade": facade_settings_rebinds_to_the_singleton_after_upgrade,
    "current_facade_resolves_after_upgrade": current_facade_resolves_after_upgrade,
    "forked_child_keeps_full_state": forked_child_keeps_full_state,
    "bad_non_lite_field_does_not_block_lite_scripts": bad_non_lite_field_does_not_block_lite_scripts,
    "field_faces_serve_while_another_thread_builds": field_faces_serve_while_another_thread_builds,
    "module_attribute_read_waits_then_serves": module_attribute_read_waits_then_serves,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
