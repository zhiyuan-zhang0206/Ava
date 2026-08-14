"""Demo: sensitive operation gate — two-tier block/warn before exec.

Motivation: GPT-5.6 was reported to autonomously delete user files, sparking
concerns about AI agents with shell access. This hook demonstrates how Ava's
before_exec hook point can serve as a safety net — intercepting dangerous
commands before they reach the shell.

Design:
- **block**: always intercepted (e.g. rm -rf /, DROP TABLE, mkfs)
- **warn**: first occurrence blocked with a reminder; second occurrence of the
  same command (same agent, same pattern) is allowed through — the agent
  "acknowledged the risk" by retrying.

Why only these two tiers? Because with a bash tool, fine-grained path-level
checks are trivially bypassed (cat -> python -c -> sed -> ... infinite).
The only meaningful distinction is irreversible destruction vs everything else.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext, agent_id_from_config
from agent.hooks import Hook, register_before_exec
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
from shared.log import logger

# ── Policy tier ────────────────────────────────────────────────────────────

BLOCK = "block"  # always intercepted
WARN = "warn"  # first time blocked, second time allowed


@dataclass(frozen=True)
class Policy:
    """One safety rule."""

    pattern: str  # regex to match in code
    tier: str  # BLOCK or WARN
    label: str  # human-readable label, e.g. "rm -rf /"
    detail: str = ""  # longer explanation


# ── Policy table ───────────────────────────────────────────────────────────
# Ordered from most destructive to least.  First match wins per policy.

POLICIES: list[Policy] = [
    # ── block: irreversible filesystem destruction ──
    Policy(r"\brm\s+-rf\s+/", BLOCK, "rm -rf /", "recursive force-delete root"),
    Policy(r"\brm\s+-rf\s+~", BLOCK, "rm -rf ~", "recursive force-delete home"),
    Policy(
        r">\s*/dev/sd[a-z]",
        BLOCK,
        "overwrite block device",
        "raw write to disk device is unrecoverable",
    ),
    Policy(r"\bmkfs\.", BLOCK, "mkfs", "format filesystem"),
    Policy(
        r"\bdd\s+if=", BLOCK, "dd disk write", "low-level disk write may overwrite partition table"
    ),
    # ── block: irreversible database destruction ──
    Policy(
        r"\bDROP\s+(TABLE|DATABASE)\b",
        BLOCK,
        "DROP TABLE / DATABASE",
        "drop database object is irreversible",
    ),
    Policy(
        r"\bTRUNCATE\s+(TABLE\s+)?", BLOCK, "TRUNCATE TABLE", "truncate table data is unrecoverable"
    ),
    # ── warn: dangerous but potentially legitimate ──
    Policy(
        r"curl\b.*\|\s*(ba)?sh\b",
        WARN,
        "curl | bash",
        "executing scripts directly from network — verify source",
    ),
    Policy(
        r"wget\b.*\|\s*(ba)?sh\b",
        WARN,
        "wget | bash",
        "executing scripts directly from network — verify source",
    ),
    Policy(r"\bsudo\s+", WARN, "sudo", "privilege escalation — confirm necessity"),
    Policy(r"\bchmod\s+777\b", WARN, "chmod 777", "open all permissions may be excessive"),
    Policy(
        r"\bgit\s+push\s+--force\b",
        WARN,
        "git push --force",
        "force push overwrites remote history",
    ),
    Policy(
        r"\bgit\s+reset\s+--hard\b",
        WARN,
        "git reset --hard",
        "hard reset loses uncommitted changes",
    ),
    Policy(r"\bkill\s+-9\b", WARN, "kill -9", "force kill — confirm not a critical process"),
    Policy(r"\bkillall\b", WARN, "killall", "batch kill — wide impact radius"),
    Policy(
        r"\bDELETE\s+FROM\s+\w+",
        WARN,
        "DELETE FROM",
        "delete database rows — confirm backup or WHERE clause",
    ),
]

# ── Warning state ──────────────────────────────────────────────────────────
# Per (agent_id, policy_index) -> True once warned.
# Survives within the process lifetime (fine for a demo; in production you'd
# persist this as a plugin state field).
_warned: dict[tuple[int, int], bool] = {}


def _hash_code(code: str) -> str:
    """Short hash of the code content, for dedup purposes."""
    return hashlib.sha256(code.encode()).hexdigest()[:12]


def _should_block(
    policy: Policy,
    agent_id: int,
    policy_idx: int,
) -> bool:
    """Decide whether to block based on tier + warning history."""
    if policy.tier == BLOCK:
        return True
    # WARN tier: block first time, allow second time
    key = (agent_id, policy_idx)
    if _warned.get(key):
        # Already warned — let through
        return False
    # First time — block and mark warned
    _warned[key] = True
    return True


def _build_block_message(
    blocked: list[tuple[Policy, int]],
    warned: list[tuple[Policy, int]],
    code_hash: str,
) -> str:
    """Build the system note displayed to the agent when blocked."""
    lines = [
        "## 🛡️ Ava Security Policy Intercept",
        "",
        "Your code triggered the security policy and was intercepted **before execution**.",
        "",
    ]

    if blocked:
        lines.append("### 🔴 Blocked (will never execute)")
        lines.append("")
        lines.append("| Operation | Detail |")
        lines.append("|-----------|--------|")
        for policy, _idx in blocked:
            detail = policy.detail or policy.label
            lines.append(f"| `{policy.label}` | {detail} |")
        lines.append("")

    if warned:
        lines.append("### 🟡 Warning (first time blocked; retry to proceed)")
        lines.append("")
        lines.append("| Operation | Detail |")
        lines.append("|-----------|--------|")
        for policy, _idx in warned:
            detail = policy.detail or policy.label
            lines.append(f"| `{policy.label}` | {detail} |")
        lines.append("")
        lines.append(
            "> If you confirm this is safe, **resubmit the same code** — it will be allowed through."
        )

    lines.append("")
    lines.append("---")
    lines.append(
        "*Background: GPT-5.6 (2026) was reported to autonomously execute `rm -rf`, "
        "`DROP TABLE` and other destructive operations on user machines. "
        "This interceptor is designed to prevent such incidents.*"
    )
    lines.append(f"<!-- gate:{code_hash} -->")

    return "\n".join(lines)


# ── Hook ───────────────────────────────────────────────────────────────────


class _SensitiveOpGateHook(Hook):
    """Scan the agent's code before execution.  Block/warn based on
    the two-tier policy table.

    Returns None when all clear; returns a dict with a warning message
    and ``goto: "after_exec"`` to skip the exec node when blocked.
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        agent_id = agent_id_from_config(config)

        # Extract code from the last AIMessage tool_calls
        last_msg = state.messages[-1] if state.messages else None
        if not isinstance(last_msg, AIMessage):
            return None
        tool_calls = getattr(last_msg, "tool_calls", None) or []
        code = ""
        for tc in tool_calls:
            if tc.get("name") == "execute_code":
                code = tc.get("args", {}).get("code", "")
                break
        if not code:
            return None

        # Scan
        blocked: list[tuple[Policy, int]] = []
        warned: list[tuple[Policy, int]] = []
        for idx, policy in enumerate(POLICIES):
            if not re.search(policy.pattern, code, re.IGNORECASE):
                continue
            if _should_block(policy, agent_id, idx):
                if policy.tier == BLOCK:
                    blocked.append((policy, idx))
                else:
                    warned.append((policy, idx))

        if not blocked and not warned:
            return None

        code_hash = _hash_code(code)
        message = _build_block_message(blocked, warned, code_hash)

        logger.info(
            "[{label}] {body}",
            label="sensitive-op-gate",
            body=(
                f"agent {agent_id} intercepted: "
                f"{len(blocked)} blocked + {len(warned)} warned, "
                f"hash={code_hash}"
            ),
        )

        return {
            "messages": [
                system_note_message(
                    content=message,
                    tag=NoteTag.SECURITY,
                    created_at=datetime.now(UTC),
                )
            ],
            "goto": "after_exec",
        }


sensitive_op_gate = _SensitiveOpGateHook()
register_before_exec(sensitive_op_gate)
