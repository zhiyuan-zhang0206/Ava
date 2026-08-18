"""`shared/birth_config.py` — what a frozen field resolves to at the spawn boundary.

The resolution chain proper (`config_overlay > birth_config > current config`) is
enforced in two halves that meet nowhere else: this module decides WHAT gets
stamped, and `agent/loop.py` decides in what ORDER the two stored maps are applied
at boot. Both halves are covered here and in `tests/agent/test_loop_main.py`.

`cluster_defaults` is a seeded singleton outside the per-test TRUNCATE, so the
shared `cluster_defaults_unset` fixture snapshots and restores it.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from shared.birth_config import (
    cluster_default_model,
    resolve_birth_config,
    set_cluster_default_model,
)
from shared.config import frozen_field_names, live_field_names


@pytest.fixture
def cur(
    db_conn: psycopg.Connection,
    cluster_defaults_unset: None,
) -> Iterator[psycopg.Cursor]:
    """A cursor on a DB whose cluster_defaults row carries no choice."""
    with db_conn.cursor() as c:
        yield c


class TestClusterDefaultModel:
    def test_unset_reads_as_none(self, cur: psycopg.Cursor) -> None:
        assert cluster_default_model(cur) is None

    def test_round_trip(self, cur: psycopg.Cursor) -> None:
        set_cluster_default_model(cur, "claude-sonnet-5", updated_by="test")
        assert cluster_default_model(cur) == "claude-sonnet-5"


class TestResolveBirthConfig:
    def test_stamps_every_frozen_field_and_no_live_one(self, cur: psycopg.Cursor) -> None:
        stamped = resolve_birth_config(cur)
        assert set(stamped) == frozen_field_names()
        assert not set(stamped) & live_field_names()

    def test_overlay_present_field_is_not_double_stamped(self, cur: psycopg.Cursor) -> None:
        """A field the spawner chose lives in config_overlay and outranks the stamp;
        copying it into birth_config too would erase the provenance the split
        exists for."""
        stamped = resolve_birth_config(cur, {"llm_model": "claude-sonnet-5"})
        assert "llm_model" not in stamped
        # Every other frozen field is still stamped.
        assert set(stamped) == frozen_field_names() - {"llm_model"}

    def test_a_live_field_in_the_overlay_changes_nothing(self, cur: psycopg.Cursor) -> None:
        stamped = resolve_birth_config(cur, {"auto_compact_fraction": 0.5})
        assert set(stamped) == frozen_field_names()

    def test_cluster_default_model_wins_over_the_config_chain(self, cur: psycopg.Cursor) -> None:
        set_cluster_default_model(cur, "claude-sonnet-5", updated_by="test")
        assert resolve_birth_config(cur)["llm_model"] == "claude-sonnet-5"

    def test_falls_through_to_the_config_chain_when_unset(self, cur: psycopg.Cursor) -> None:
        from shared.config import settings

        assert resolve_birth_config(cur)["llm_model"] == settings.lm.llm_model

    def test_none_is_stamped_not_dropped(self, cur: psycopg.Cursor) -> None:
        """For a per-model-defaultable field, None is the "the cluster expressed no
        opinion" sentinel. Freezing THAT is what keeps a later explicit cluster
        choice from reaching this agent, so the key must be present with a null."""
        stamped = resolve_birth_config(cur)
        assert "reasoning_effort" in stamped
        assert stamped["reasoning_effort"] is None


class TestForkInheritance:
    def test_parent_stamp_is_carried_over_verbatim(self, cur: psycopg.Cursor) -> None:
        """A fork is the same identity continuing — it must not silently re-resolve
        its brain against today's defaults."""
        parent = dict.fromkeys(frozen_field_names(), "PARENT")
        assert resolve_birth_config(cur, inherited=parent) == parent

    def test_a_frozen_field_the_parent_predates_is_resolved_fresh(
        self, cur: psycopg.Cursor
    ) -> None:
        """An old agent's stamp does not cover a frozen field added since. The fork
        is still fully stamped rather than left with a hole that resolves live."""
        partial = {"llm_model": "claude-sonnet-5"}
        stamped = resolve_birth_config(cur, inherited=partial)
        assert stamped["llm_model"] == "claude-sonnet-5"
        assert set(stamped) == frozen_field_names()

    def test_the_forks_own_overlay_still_outranks_the_inherited_stamp(
        self, cur: psycopg.Cursor
    ) -> None:
        """The inherited value stays on the row (it is the parent's birth fact); the
        fork's explicit choice wins at apply time, not by erasing the stamp."""
        parent = {"llm_model": "claude-sonnet-5"}
        stamped = resolve_birth_config(cur, {"llm_model": "deepseek-v4-flash"}, inherited=parent)
        assert stamped["llm_model"] == "claude-sonnet-5"
