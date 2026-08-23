"""Assemble one self-evolution trace dataset record.

This script sibling owns the record-shaping helpers used by ``collect.py``.
Keeping the assembly here separates collection transport and database access
from the stable dataset-record contract.
"""

from __future__ import annotations

import ast
import contextlib
import os
import sys
from collections import Counter
from typing import Any

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for sibling imports (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
from audit import LeakPaths, invalidated, scan
from label import label  # sibling script, resolved via sys.path[0]
from pydantic import ValidationError

from shared.audit_events import SkillInvokedPayload
from shared.checkpoint import CheckpointReadError, load_checkpoint_messages_full

# Chinese correction keywords — user is redirecting or correcting the agent.
CN_CORRECTION_KEYWORDS = [
    "不对",
    "错了",
    "重新",
    "不要",
    "改成",
    "应该是",
    "不是这样",
    "你搞错了",
    "搞错了",
    "纠正",
    "修正一下",
    "不是这个",
    "别这样",
    "不是让你",
    "你没理解",
    "理解错了",
    "错误",
    "有误",
    "误导",
]

# English correction keywords — user is redirecting or correcting the agent.
EN_CORRECTION_KEYWORDS = [
    "wrong",
    "incorrect",
    "don't",
    "do not",
    "should be",
    "instead",
    "actually",
    "no,",
    "fix this",
    "correct this",
    "not right",
    "mistake",
    "you misunderstood",
    "that's not",
    "redo",
    "re-do",
    "try again",
]

# Agent-to-agent feedback keywords — one agent correcting or reminding another.
PEER_FEEDBACK_KEYWORDS = [
    "你那个",
    "你没",
    "不要用",
    "别用",
    "改成",
    "修一下",
    "还没修",
    "没修好",
    "不对",
    "错了",
    "应该用",
    "不能这样",
    "注意",
    "别忘了",
    "提醒",
    "纠正",
]


def _detect_correction(content: str) -> bool:
    """Return whether content reads as a user correction rather than a follow-up."""
    if not content:
        return False
    if any(keyword in content for keyword in CN_CORRECTION_KEYWORDS):
        return True
    content_lower = content.lower()
    return any(keyword in content_lower for keyword in EN_CORRECTION_KEYWORDS)


def _detect_peer_feedback(content: str) -> bool:
    """Return whether an agent-to-agent message contains corrective feedback."""
    if not content:
        return False
    if any(keyword in content for keyword in PEER_FEEDBACK_KEYWORDS):
        return True
    return _detect_correction(content)


def _dotted(node: ast.AST) -> str | None:
    """Resolve an attribute chain like ``ava.files.edit`` to its dotted string."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def count_tool_calls(codes: list[str]) -> dict[str, int]:
    """Count ``ava.*`` SDK calls across a run's executed code blocks."""
    counts: Counter[str] = Counter()
    for code in codes:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name and name.split(".", 1)[0] == "ava":
                    counts[name] += 1
    return dict(counts)


def _is_exec_ok(event: str) -> bool:
    return event == "exec"


def _is_exec_fail(event: str) -> bool:
    return event != "exec" and event.startswith(("exec_", "exec("))


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("type") or "")
            else:
                parts.append(str(block))
        return " ".join(part for part in parts if part)
    return str(content)


def _transcript(agent_id: int) -> list[dict[str, str]]:
    """Full Task #1125 conversation, or [] if its checkpoint is unreadable."""
    try:
        messages = load_checkpoint_messages_full(agent_id)
    except CheckpointReadError:
        return []
    return [{"type": message.type, "content": _msg_text(message.content)} for message in messages]


def _skills_touched(log_events: list[tuple]) -> list[str]:
    """Return explicitly loaded skills recorded by the run."""
    loaded: set[str] = set()
    for event_type, payload in log_events:
        if event_type != "skill_invoked":
            continue
        with contextlib.suppress(ValidationError):
            entry = SkillInvokedPayload.model_validate(payload)
            if entry.invocation_depth == "loaded":
                loaded.add(entry.skill)
    return sorted(loaded)


def _plugins_activated(events: list[tuple]) -> tuple[dict[str, int], int]:
    """Return fired plugin contribution counts and malformed-row count."""
    fired: Counter[str] = Counter()
    skipped = 0
    for event_type, payload in events:
        if event_type != "plugin_activation":
            continue
        data = payload or {}
        plugin, surface, identifier = (
            data.get("plugin"),
            data.get("surface"),
            data.get("identifier"),
        )
        if not (plugin and surface and identifier):
            skipped += 1
            continue
        fired[f"{plugin}/{surface}/{identifier}"] += 1
    return dict(sorted(fired.items())), skipped


def _mode(values: list[str]) -> str:
    return Counter(value for value in values if value).most_common(1)[0][0] if any(values) else ""


def build_record(
    agent_id: int,
    week: str,
    events: list[tuple],
    log_events: list[tuple],
    inbounds: list[dict[str, Any]],
    meta: tuple,
    *,
    leak_paths: LeakPaths | None = None,
) -> dict[str, Any]:
    """Assemble the stable trace dataset record for one agent run."""
    spawner, status, last_message_text = meta

    user_messages = [message["content"] for message in inbounds if message["source"] == "user"]
    agent_messages = [message for message in inbounds if message["source"].startswith("agent:")]

    task_prompt = user_messages[0] if user_messages else ""
    if not task_prompt.strip() and spawner.startswith("agent:"):
        spawner_messages = [message for message in agent_messages if message["source"] == spawner]
        if spawner_messages:
            task_prompt = spawner_messages[0]["content"]
    if not task_prompt.strip():
        system_messages = [
            message["content"] for message in inbounds if message["source"] == "system"
        ]
        if system_messages:
            task_prompt = system_messages[0]

    corrections: list[str] = []
    followup_prompts: list[str] = []
    for message in user_messages[1:]:
        if _detect_correction(message):
            corrections.append(message)
        else:
            followup_prompts.append(message)

    spawner_id: int | None = None
    if spawner.startswith("agent:"):
        with contextlib.suppress(IndexError, ValueError):
            spawner_id = int(spawner.split(":", 1)[1])
    peer_feedback: list[dict[str, Any]] = []
    for message in agent_messages:
        try:
            from_agent = int(message["source"].split(":", 1)[1])
        except (IndexError, ValueError):
            from_agent = 0
        if from_agent == spawner_id:
            continue
        if _detect_peer_feedback(message["content"]):
            peer_feedback.append({"from_agent": from_agent, "content": message["content"]})

    turns = sum(1 for event, _ in events if event == "turn_end")
    models = [str(payload.get("model", "")) for event, payload in events if event == "llm_usage"]
    tokens_in = sum(
        int(payload.get("in_total", 0)) for event, payload in events if event == "llm_usage"
    )
    tokens_out = sum(
        int(payload.get("out_total", 0)) for event, payload in events if event == "llm_usage"
    )
    code_bodies = [str(payload.get("body", "")) for event, payload in events if event == "code"]
    exec_ok = sum(1 for event, _ in events if _is_exec_ok(event))
    exec_failed = sum(1 for event, _ in events if _is_exec_fail(event))
    exec_outcomes = [event for event, _ in events if _is_exec_ok(event) or _is_exec_fail(event)]
    last_exec_failed = bool(exec_outcomes) and _is_exec_fail(exec_outcomes[-1])
    durations = [
        float(payload.get("duration_seconds", 0))
        for event, payload in events
        if event == "turn_end"
    ]
    exec_duration_s = sum(
        float((payload or {}).get("duration_seconds", 0))
        for event, payload in events
        if event == "node_exit" and (payload or {}).get("node") == "exec"
    )

    compactions = sum(1 for event_type, _ in log_events if event_type == "compact")
    breached = any(event_type == "report_breached" for event_type, _ in log_events)
    transcript = _transcript(agent_id)

    plugins_activated, plugins_activated_skipped = _plugins_activated(events)
    if plugins_activated_skipped:
        print(
            f"warning: agent {agent_id}: skipped {plugins_activated_skipped} "
            "malformed plugin_activation row(s)"
        )

    record: dict[str, Any] = {
        "agent_id": agent_id,
        "week": week,
        "spawner": spawner,
        "model": _mode(models),
        "task_prompt": task_prompt,
        "followup_prompts": followup_prompts,
        "corrections": corrections,
        "peer_feedback": peer_feedback,
        "transcript": transcript,
        "final_output": last_message_text or "",
        "turns": turns,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tools_called": count_tool_calls(code_bodies),
        "skills_touched": _skills_touched(log_events),
        "plugins_activated": plugins_activated,
        "plugins_activated_skipped": plugins_activated_skipped,
        "exec_ok": exec_ok,
        "exec_failed": exec_failed,
        "last_exec_failed": last_exec_failed,
        "compactions": compactions,
        "breached": breached,
        "terminated": status == "terminated",
        "duration_s": round(sum(durations), 1),
    }
    if leak_paths is not None:
        findings = scan(code_bodies, agent_id=agent_id, leak_paths=leak_paths)
        record["leak_audit"] = [
            {"surface": finding.surface, "evidence": finding.evidence, "tool": finding.tool}
            for finding in findings
        ]
        record["invalidated"] = invalidated(findings)
    record["label"] = label(record)
    return record
