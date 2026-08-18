"""Eval/harness environment — AgentEvalSettings.

Container-mode flags (in-container mount + output dir) and the security-scan gate hermetic benchmark environments disable. Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field

from shared.config._base import EnvSettings


class AgentEvalSettings(EnvSettings):
    eval_output_dir: Path = Field(
        default=Path("/workspace"),
        alias="AVA_OUTPUT_DIR",
        description="Directory inside the eval container for writing result.json.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    eval_container_exec: bool = Field(
        default=False,
        alias="AVA_CONTAINER_EXEC",
        description="Whether the process runs inside the eval container. True switches ctx_builder to the in-container mount and defers check to the host judge.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    security_scan_enabled: bool = Field(
        default=True,
        alias="AVA_SECURITY_SCAN_ENABLED",
        validation_alias=AliasChoices("AVA_SECURITY_SCAN_ENABLED", "AVA_SKIP_SECURITY_SCAN"),
        description=(
            "Enable prompt-injection security scan on inbound chat. "
            "Set false for benchmark / hermetic environments where all input is trusted. "
            "The legacy AVA_SKIP_SECURITY_SCAN alias has INVERTED semantics "
            "(AVA_SKIP_SECURITY_SCAN=true means this is false); dotenv_boot "
            "translates it at load and converge renames it."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
