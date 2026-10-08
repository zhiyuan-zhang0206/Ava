"""ava.sdk_surface.agent_identity — the identity read off the bound context, and the owns_loop guard.

The guard is the safety behavior the launched-script work added: a background
script (owns_loop=False) must not drive the agent's turn loop, so the lifecycle
self-actions refuse there. These tests lock all four guard sites + the flag
logic so a future edit that drops a guard call or inverts the condition (which
would silently reopen the watcher-compacts-its-own-agent bug) fails loudly.
"""

import pytest

import ava
from ava.sdk_surface import agent_identity
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


def _owns_loop() -> bool:
    """Whether the bound context's identity owns the turn loop."""
    context = ava.context
    assert context.identity is not None
    return context.identity.owns_loop


def test_bound_context_sets_identity_and_flag() -> None:
    pin_agent(99, owns_loop=False)
    assert ava.self.AGENT_ID == 99
    assert _owns_loop() is False

    pin_agent(7, owns_loop=True)
    assert ava.self.AGENT_ID == 7
    assert _owns_loop() is True


def test_assert_self_action_permits_bootstrapped_agent() -> None:
    pin_agent(1, owns_loop=True)
    agent_identity.assert_self_action("compact")  # no raise


def test_assert_self_action_refuses_when_not_loop_owner() -> None:
    pin_agent(1, owns_loop=False)
    with pytest.raises(RuntimeError, match="can only be called from inside an agent process"):
        agent_identity.assert_self_action("compact")


def test_assert_self_action_refuses_when_identity_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # owns_loop True (default) but no identity — an ad-hoc `python -c "import ava"`
    # that never bootstrapped. Must fail fast here, not defer to a null-agent_id
    # INSERT hitting the DB NOT NULL constraint.
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="established agent identity"):
        agent_identity.assert_self_action("terminate")


def test_require_agent_id_returns_when_set() -> None:
    pin_agent(42, owns_loop=True)
    assert agent_identity.require_agent_id() == 42


def test_require_agent_id_raises_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="no established agent identity"):
        agent_identity.require_agent_id()


def test_context_from_env_sets_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert agent_identity.agent_id() == 42
    assert _owns_loop() is False  # env-established scripts don't own the loop


def test_context_from_env_noop_when_already_set(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_agent(7, owns_loop=True)
    monkeypatch.setenv("AVA_AGENT_ID", "99")  # should be ignored
    assert agent_identity.agent_id() == 7  # already-established wins
    assert _owns_loop() is True  # unchanged


def test_is_launched_child_true_for_env_established(monkeypatch: pytest.MonkeyPatch) -> None:
    # A watcher / persistent-shell / schedule child: AVA_AGENT_ID forwarded in the
    # env, no explicit establish. is_launched_child establishes from env (as a
    # non-owner) and reports True — this is the signal that gates the lazy
    # plugin-namespace load in ava.__getattr__.
    pin_no_identity()
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert agent_identity.is_launched_child() is True
    assert _owns_loop() is False  # env-established scripts don't own the loop


def test_is_launched_child_false_for_agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real agent process owns the turn loop — it is never a "launched child",
    # so it never lazily reloads plugins (which would clear its built-in hooks).
    monkeypatch.setenv("AVA_AGENT_ID", "7")
    pin_agent(7, owns_loop=True)
    assert agent_identity.is_launched_child() is False


def test_is_launched_child_false_without_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # gateway / cli / ad-hoc `python -c`: no AVA_AGENT_ID, no identity -> not a
    # child, so ava.__getattr__ keeps fail-fast on unknown ava.X.
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert agent_identity.is_launched_child() is False


def test_actor_sets_system_principal() -> None:
    pin_agent(None, actor="schedule:7")
    assert agent_identity.require_actor() == "schedule:7"
    assert agent_identity.default_actor() == "schedule:7"


def test_require_actor_derives_agent_when_no_actor() -> None:
    pin_agent(42, owns_loop=True)  # no actor
    assert agent_identity.require_actor() == "agent:42"
    assert agent_identity.default_actor() == "agent:42"


def test_actor_takes_precedence_over_agent_id() -> None:
    pin_agent(5, owns_loop=True, actor="schedule:9")
    assert agent_identity.require_actor() == "schedule:9"


def test_require_actor_raises_with_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="no established actor or agent identity"):
        agent_identity.require_actor()


def test_require_actor_binds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fix: require_actor() must bind the launch-env context so a
    # watcher/background script that has AVA_AGENT_ID in its environment
    # (forwarded by the session machinery) can use send_message/spawn without
    # first accessing ava.self.AGENT_ID.
    pin_no_identity()
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert agent_identity.require_actor() == "agent:42"
    assert _owns_loop() is False  # env-established, not loop owner


def test_default_actor_binds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # default_actor() should also establish from env for consistency --
    # without it, a watcher calling terminate/restart/resurrect would
    # attribute the action to "agent:None".
    pin_no_identity()
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    assert agent_identity.default_actor() == "agent:42"
    assert _owns_loop() is False


def test_default_actor_is_non_raising_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lower-stakes default-source paths keep the pre-actor behavior rather
    # than raising: no identity at all -> the "agent:None" sentinel string.
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert agent_identity.default_actor() == "agent:None"


@pytest.mark.parametrize("action", ["compact", "restart", "terminate"])
def test_lifecycle_self_actions_refuse_from_launched_script(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_agent(1, owns_loop=False)

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


def test_context_outside_a_bound_process_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like `ava.state` outside an exec turn: no bound context, no attribute."""
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    with pytest.raises(AttributeError, match=r"ava\.context requires an execution child"):
        _ = ava.context
    assert getattr(ava, "context", None) is None


def test_context_exposes_the_bound_identity() -> None:
    pin_agent(31, owns_loop=False, actor="schedule:3")
    assert ava.context.identity is not None
    assert (ava.context.identity.agent_id, ava.context.identity.owns_loop) == (31, False)
    assert ava.context.identity.actor == "schedule:3"


def test_launched_script_context_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin_no_identity()
    monkeypatch.setenv("AVA_AGENT_ID", "58")
    assert ava.context.identity is not None
    assert (ava.context.identity.agent_id, ava.context.identity.owns_loop) == (58, False)


def test_bare_script_has_no_context() -> None:
    """A process nobody launched as an agent: no bound context, and `ava.context` does not exist
    (the way `ava.state` does not exist outside an exec turn)."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "AVA_AGENT_ID"}
    proc = subprocess.run(
        [sys.executable, "-I", "-c", "import ava\nprint(hasattr(ava, 'context'))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"
