"""Eval/harness environment — AgentEvalSettings.

Container-mode flags (in-container mount + output dir) and the security-scan gate hermetic benchmark environments disable. Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import NoDecode

from shared.config._base import EnvSettings


class AgentEvalSettings(EnvSettings):
    eval_isolation: bool = Field(
        default=False,
        alias="AVA_EVAL_ISOLATION",
        description=(
            "Isolate an evaluation agent from shared memory, network-facing SDK "
            "capabilities, and peer-result reads."
        ),
        json_schema_extra={
            "per_agent": True,
            "lifecycle": "frozen",
            "restart_required": "agent",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    eval_network_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="AVA_EVAL_NETWORK_ALLOWLIST",
        description=(
            "Comma-separated network-facing SDK capabilities an isolated evaluation "
            "agent may use: `web` and `understand`."
        ),
        json_schema_extra={
            "per_agent": True,
            "lifecycle": "frozen",
            "restart_required": "agent",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

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

    @field_validator("eval_network_allowlist", mode="before")
    @classmethod
    def _split_eval_network_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("eval_network_allowlist")
    @classmethod
    def _validate_eval_network_allowlist(cls, value: list[str]) -> list[str]:
        unsupported = sorted(set(value) - {"web", "understand"})
        if unsupported:
            raise ValueError(
                "eval network allowlist only accepts 'web' and 'understand'; "
                f"unsupported entries: {unsupported}"
            )
        return value
