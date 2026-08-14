"""ava._boot — the one-input identity bootstrap and the owns_loop guard.

The guard is the safety behavior the launched-script work added: a background
script (owns_loop=False) must not drive the agent's turn loop, so the lifecycle
self-actions refuse there. These tests lock all four guard sites + the flag
logic so a future edit that drops a guard call or inverts the condition (which
would silently reopen the watcher-compacts-its-own-agent bug) fails loudly.
"""

import pytest

import ava
import ava._boot as boot


@pytest.fixture(autouse=True)
def _restore_boot_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # establish() mutates module-global _owns_loop and _agent_id; snapshot both
    # (no-op set captures the current value) so monkeypatch restores them at
    # teardown regardless of what establish reassigned.
    monkeypatch.setattr(boot, "_owns_loop", boot._owns_loop)
    monkeypatch.setattr(boot, "_agent_id", boot._agent_id)
    monkeypatch.setattr(boot, "_actor", boot._actor)


def test_establish_sets_identity_and_flag() -> None:
    boot.establish(99, owns_loop=False)
    assert ava.self.AGENT_ID == 99
    assert boot._owns_loop is False

    boot.establish(7, owns_loop=True)
    assert ava.self.AGENT_ID == 7
    assert boot._owns_loop is True


def test_assert_self_action_permits_bootstrapped_agent() -> None:
    boot.establish(1, owns_loop=True)
    boot.assert_self_action("compact")  # no raise


def test_assert_self_action_refuses_when_not_loop_owner() -> None:
    boot.establish(1, owns_loop=False)
    with pytest.raises(RuntimeError, match="can only be called from inside an agent process"):
        boot.assert_self_action("compact")


def test_assert_self_action_refuses_when_identity_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # owns_loop True (default) but no identity — an ad-hoc `python -c "import ava"`
    # that never bootstrapped. Must fail fast here, not defer to a null-agent_id
    # INSERT hitting the DB NOT NULL constraint.
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="established agent identity"):
        boot.assert_self_action("terminate")


def test_require_agent_id_returns_when_set() -> None:
    boot.establish(42, owns_loop=True)
    assert boot.require_agent_id() == 42


def test_require_agent_id_raises_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="no established agent identity"):
        boot.require_agent_id()


def test_try_establish_from_env_sets_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert boot.agent_id() == 42
    assert boot._owns_loop is False  # env-established scripts don't own the loop


def test_try_establish_from_env_noop_when_already_set(monkeypatch: pytest.MonkeyPatch) -> None:
    boot.establish(7, owns_loop=True)
    monkeypatch.setenv("AVA_AGENT_ID", "99")  # should be ignored
    assert boot.agent_id() == 7  # already-established wins
    assert boot._owns_loop is True  # unchanged


def test_is_launched_child_true_for_env_established(monkeypatch: pytest.MonkeyPatch) -> None:
    # A watcher / persistent-shell / schedule child: AVA_AGENT_ID forwarded in the
    # env, no explicit establish. is_launched_child establishes from env (as a
    # non-owner) and reports True — this is the signal that gates the lazy
    # plugin-namespace load in ava.__getattr__.
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert boot.is_launched_child() is True
    assert boot._owns_loop is False  # env-established scripts don't own the loop


def test_is_launched_child_false_for_agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real agent process owns the turn loop — it is never a "launched child",
    # so it never lazily reloads plugins (which would clear its built-in hooks).
    monkeypatch.setenv("AVA_AGENT_ID", "7")
    boot.establish(7, owns_loop=True)
    assert boot.is_launched_child() is False


def test_is_launched_child_false_without_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # gateway / cli / ad-hoc `python -c`: no AVA_AGENT_ID, no identity -> not a
    # child, so ava.__getattr__ keeps fail-fast on unknown ava.X.
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert boot.is_launched_child() is False


def test_establish_actor_sets_system_principal() -> None:
    boot.establish_actor("schedule:7")
    assert boot.require_actor() == "schedule:7"
    assert boot.default_actor() == "schedule:7"


def test_require_actor_derives_agent_when_no_actor() -> None:
    boot.establish(42, owns_loop=True)  # no establish_actor
    assert boot.require_actor() == "agent:42"
    assert boot.default_actor() == "agent:42"


def test_actor_takes_precedence_over_agent_id() -> None:
    boot.establish(5, owns_loop=True)
    boot.establish_actor("schedule:9")
    assert boot.require_actor() == "schedule:9"


def test_require_actor_raises_with_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot, "_actor", None)
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="no established actor or agent identity"):
        boot.require_actor()


def test_require_actor_establishes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fix: require_actor() must call _try_establish_from_env() so a
    # watcher/background script that has AVA_AGENT_ID in its environment
    # (forwarded by the session machinery) can use send_message/spawn without
    # first accessing ava.self.AGENT_ID.
    monkeypatch.setattr(boot, "_actor", None)
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert boot.require_actor() == "agent:42"
    assert boot._owns_loop is False  # env-established, not loop owner


def test_default_actor_establishes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # default_actor() should also establish from env for consistency --
    # without it, a watcher calling terminate/restart/resurrect would
    # attribute the action to "agent:None".
    monkeypatch.setattr(boot, "_actor", None)
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert boot.default_actor() == "agent:42"
    assert boot._owns_loop is False


def test_default_actor_is_non_raising_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lower-stakes default-source paths keep the pre-actor behavior rather
    # than raising: no identity at all -> the "agent:None" sentinel string.
    monkeypatch.setattr(boot, "_actor", None)
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert boot.default_actor() == "agent:None"


@pytest.mark.parametrize("action", ["compact", "restart", "terminate"])
def test_lifecycle_self_actions_refuse_from_launched_script(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot.establish(1, owns_loop=False)

    # Tripwire: if the guard were missing, the action would reach DB / gateway —
    # blow up loudly there rather than let the test pass on a silent regression.
    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError(f"ava.self.{action} reached DB despite owns_loop=False")

    monkeypatch.setattr(ava.DB, "cursor", _boom)

    fn = getattr(ava.self, action)
    with pytest.raises(RuntimeError, match="can only be called from inside an agent process"):
        fn("summary") if action == "compact" else fn()


def test_update_removed_points_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """ava.self.update() was removed (2026-08): it now raises unconditionally with
    the CLI replacement, never touching DB / gateway."""
    monkeypatch.setattr(
        ava.DB,
        "cursor",
        _boom_cursor := lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("reached DB")),  # pyright: ignore[reportUnknownArgumentType]
    )

    with pytest.raises(RuntimeError, match=r"ava\.self\.update\(\) has been removed"):
        ava.self.update()
