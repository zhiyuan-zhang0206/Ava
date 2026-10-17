"""Build the `UnderstandDeps` of this process: its models, config, provider factory and workspace."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from base.lm import factory
from base.paths import workspace_dir
from meiva import files
from meiva.sdk_surface import agent_identity
from meiva.sdk_surface import settings as sdk_settings
from meiva.sdk_surface.settings import config_authority
from meiva.security import scan_content
from meiva.understand.answer import OVERFLOW_DIRNAME, RetryPolicy, UnderstandDeps

__all__ = ["understand_deps"]


def _output_dir() -> Path | None:
    """The bound agent's `.exec_output/` directory, or None outside an agent process."""
    try:
        agent_id = agent_identity.require_agent_id()
    except RuntimeError:
        return None
    return workspace_dir(agent_id) / OVERFLOW_DIRNAME


def _now() -> datetime:
    return datetime.now(sdk_settings.clock().explicit_zone())


def understand_deps() -> UnderstandDeps:
    """The deps for one `meiva.understand` call, read from this process's SDK installation."""
    authority = config_authority()
    return UnderstandDeps(
        catalog=sdk_settings.model_catalog(),
        overrides=sdk_settings.model_overrides(),
        text_model=authority.service_field_value("understand_text_model"),
        media_model=authority.service_field_value("understand_media_model"),
        media_resolution=authority.service_field_value("understand_media_resolution"),
        media_thinking_level=authority.service_field_value("understand_media_thinking_level"),
        media_base_url=authority.service_field_value("understand_media_base_url"),
        llm_override=authority.service_field_value("llm_override"),
        timeout_seconds=authority.service_field_value("llm_invoke_timeout_seconds"),
        retry=RetryPolicy(
            attempts=authority.service_field_value("llm_invoke_retry_attempts"),
            delay_seconds=authority.service_field_value("llm_invoke_retry_delay_seconds"),
            max_delay_seconds=authority.service_field_value("llm_invoke_retry_max_delay_seconds"),
        ),
        build_chat_model=factory.build_chat_model,
        scan=scan_content,
        resolve=files.resolve,
        output_dir=_output_dir,
        now=_now,
    )
