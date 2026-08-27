"""Sandbox config — SandboxSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from shared.config._base import EnvSettings


class SandboxSettings(EnvSettings):
    @model_validator(mode="after")
    def _check_exec_timeout_relation(self) -> Self:
        """Inner code-exec timeout must be strictly less than the outer
        node-level timeout, so normal code timeouts are caught by the inner
        layer and returned to the agent; the outer shield only fires on a
        hang the inner deadline missed. A violated relation is a config
        error the operator must fix.

        Lives here (not in shared/config/__init__.py) so the check only
        runs when SandboxSettings is actually constructed — a gateway
        process that skips sandbox won't trigger it.
        """
        if self.exec_timeout_seconds >= self.exec_node_timeout_seconds:
            raise ValueError(
                f"exec_timeout_seconds ({self.exec_timeout_seconds}s) must be less than "
                f"exec_node_timeout_seconds ({self.exec_node_timeout_seconds}s) — "
                f"inner code timeout must fire before outer node timeout"
            )
        return self

    @model_validator(mode="after")
    def _check_exec_output_cap_relation(self) -> Self:
        """The accumulation budget must not sit below the inline envelope cap.

        The accumulator keeps half its budget at each end; the envelope's
        `truncate_both_ends` then slices `exec_output_max_chars // 2` off each
        end. A budget below the inline cap would hand the envelope less than it
        slices, so its "head" would reach into the accumulator's dropped middle
        and the two layers would disagree about what survived. A violated
        relation is a config error the operator must fix.
        """
        if self.exec_output_accumulation_max_chars < self.exec_output_max_chars:
            raise ValueError(
                f"exec_output_accumulation_max_chars "
                f"({self.exec_output_accumulation_max_chars}) must be >= "
                f"exec_output_max_chars ({self.exec_output_max_chars}) — the inline "
                f"envelope must still have both ends of the accumulated output to render"
            )
        return self

    exec_timeout_seconds: float = Field(
        default=300.0,
        alias="AVA_EXEC_TIMEOUT_SECONDS",
        description="Hard timeout (seconds) for a single execute_code. On timeout, the envelope hints the agent toward ava.shell.run_background / ava.watcher.launch for long-running work. Must be less than exec_node_timeout_seconds (validated at startup).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    exec_node_timeout_seconds: float = Field(
        default=1200.0,
        alias="AVA_EXEC_NODE_TIMEOUT_SECONDS",
        description=(
            "Graph-level timeout (seconds) wrapping the exec node's code-execution "
            "await. Defense-in-depth over exec_timeout_seconds: catches a hang inside "
            "the execution machinery the inner deadline missed."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    exec_output_max_chars: int = Field(
        default=30_000,
        alias="AVA_EXEC_OUTPUT_MAX_CHARS",
        description="Inline character cap on a single exec's output fed back to the LLM. On overflow, keep the first + last half, drop the middle, and write the full output to a tmp file whose path is reported.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    exec_output_accumulation_max_chars: int = Field(
        default=1_000_000,
        alias="AVA_EXEC_OUTPUT_ACCUMULATION_MAX_CHARS",
        description="Character budget the exec output accumulator holds in memory WHILE code runs. Past it the first half + last half are kept and the middle is dropped as it streams, so a runaway print loop cannot pressure the agent process; execution continues and the agent sees the drop marker. Must be >= exec_output_max_chars (validated at startup).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    syntax_fix_ruff_format: bool = Field(
        default=True,
        alias="AVA_SYNTAX_FIX_RUFF_FORMAT",
        description="Run `ruff format` on each code block before exec (after deterministic syntax fixes), keeping in-context code canonical. Toggle for ablation.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    mcp_connect_timeout_seconds: float = Field(
        default=60.0,
        alias="AVA_MCP_CONNECT_TIMEOUT_SECONDS",
        description="MCP server connect timeout (seconds). npm package downloads / uvx venv setup may be slow; 60s leaves headroom.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    mcp_daemon_start_timeout_seconds: float = Field(
        default=30.0,
        alias="AVA_MCP_DAEMON_START_TIMEOUT_SECONDS",
        description="Timeout (seconds) for the in-process MCP daemon subprocess to reach ready.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    mcp_daemon_stop_timeout_seconds: float = Field(
        default=5.0,
        alias="AVA_MCP_DAEMON_STOP_TIMEOUT_SECONDS",
        description="Graceful-stop timeout for the in-process MCP daemon; kill -9 after this (seconds).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
