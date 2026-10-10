"""Effective model validation preserves birth pins and rejects incompatible edits."""

from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.agents import InvalidModelConfig
from base.agents.birth_config import set_cluster_default_model
from base.config import Settings, settings
from base.config.service_read import ConfigAuthority
from base.db import create_agent
from base.lm.catalog import ModelCatalog
from base.lm.model_config import validate_restart_model_config, validate_spawn_model_config


def test_fork_preflight_keeps_its_birth_model_after_default_changes(
    db_conn: psycopg.Connection,
    cluster_defaults_unset: None,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,birth_config) VALUES(%s,'idling','model-config-test',%s)",
        (agent_id, Jsonb({"llm_model": "deepseek-flash", "reasoning_effort": "max"})),
    )
    with db_conn.cursor() as cur:
        set_cluster_default_model(cur, "mimo-v2.6-pro", updated_by="test")
        assert (
            validate_spawn_model_config(
                cur,
                None,
                agent_id,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                authority=ConfigAuthority(settings, Settings(profile=None), tmp_path / ".env"),
            )
            == "deepseek-flash"
        )


def test_restart_validates_birth_effort_when_only_the_model_changes(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,birth_config) VALUES(%s,'idling','model-config-test',%s)",
        (agent_id, Jsonb({"llm_model": "gpt-5.6-sol", "reasoning_effort": "low"})),
    )
    with db_conn.cursor() as cur:
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            validate_restart_model_config(
                cur,
                agent_id,
                {"llm_model": "deepseek-flash"},
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                default_model=settings.lm.llm_model,
            )
        # An explicit replacement fixes the inherited mismatch.
        validate_restart_model_config(
            cur,
            agent_id,
            {"llm_model": "deepseek-flash", "reasoning_effort": "max"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            default_model=settings.lm.llm_model,
        )


def test_legacy_restart_validates_effort_against_the_unpinned_model(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setattr(settings.lm, "llm_model", "mimo-v2.6-pro")
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling','model-config-test')",
        (agent_id,),
    )
    with db_conn.cursor() as cur:
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            validate_restart_model_config(
                cur,
                agent_id,
                {"reasoning_effort": "max"},
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                default_model=settings.lm.llm_model,
            )
        validate_restart_model_config(
            cur,
            agent_id,
            {"reasoning_effort": "high"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            default_model=settings.lm.llm_model,
        )
