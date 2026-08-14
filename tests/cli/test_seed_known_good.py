"""`_seed_known_good_if_null` — the cli glue that floors `last_known_good_sha` on
the first successful start. Gateway-only; resolves HEAD then delegates to the
shared idempotent seed. Best-effort: it must never raise out of the start path.

The shared seed logic itself (NULL -> seed, already-set -> no-op, provenance,
singleton guard) is covered in tests/shared/test_cluster_pin.py; here we only pin
the cli wiring, so these run without a DB or a real git checkout."""

from __future__ import annotations

import pytest

import cli.commands._update_git as _git
import cli.commands.start as _start
import shared.cluster_pin as _pin


def test_gateway_seeds_via_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway role: resolves HEAD and hands it to the shared seed fn, tagged."""
    monkeypatch.setattr(_git, "git_head_sha", lambda: "deadbeefcafe")
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        _pin,
        "seed_last_known_good_sha_if_null",
        lambda sha, *, set_by=None: bool(seen.append((sha, set_by))) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    _start._seed_known_good_if_null(frozenset({"gateway", "agent-runner"}))
    assert seen == [("deadbeefcafe", "seed-on-first-start")]


def test_agent_runner_only_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-gateway host never touches the central pin — its HEAD is not
    authoritative for what the cluster runs."""
    called = False

    def _boom(*_a, **_k) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(_pin, "seed_last_known_good_sha_if_null", _boom)  # pyright: ignore[reportUnknownArgumentType]
    _start._seed_known_good_if_null(frozenset({"agent-runner"}))
    assert called is False


def test_best_effort_swallows_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A git / DB failure while seeding must not raise out of the start path."""

    def _raise() -> str:
        raise _git.GitPullFailed("no HEAD")

    monkeypatch.setattr(_git, "git_head_sha", _raise)
    _start._seed_known_good_if_null(frozenset({"gateway"}))  # must not raise
    assert "last_known_good seed skipped" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
