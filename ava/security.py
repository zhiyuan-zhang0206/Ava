"""Rule-based scan that marks ingested content carrying prompt-injection
patterns; pure string matching, no model call. Findings ride along as system
notes on the affected turn — the content itself is returned clean.

A mitigation, not a boundary: a clean result means "no known pattern
matched", not "safe".
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all_for_ava__ = [
    "SecurityFindingEntry",
    "is_flagged",
    "scan_content",
    "take_findings",
]

# Structural markup an attacker uses to forge a system message or a tool call.
# Matched case-insensitively as a plain substring; these strings do not occur in
# ordinary prose, so the false-positive rate is near zero.
_MARKUP = (
    "<function_calls>",
    "</function_calls>",
    "<invoke>",
    "</invoke>",
    "<tool_calls>",
    "</tool_calls>",
    "<tool_call>",
    "[system]",
    "[system prompt]",
)

# Direct imperatives that try to override or leak the standing instructions.
# Kept deliberately specific: broad role-framing ("you are a", "you are now",
# "pretend you are", "act as if you are") is omitted on purpose, it fires on
# ordinary first-party text while adding little signal against a model already
# trained to resist it.
_IMPERATIVES = (
    "ignore previous instructions",
    "ignore all previous",
    "forget all previous",
    "print your system prompt",
    "reveal your instructions",
    "what are your instructions",
    "show me your prompts",
    "your system message",
    "your original instructions",
    "from now on you are",
    "you are now dan",
)

# Invisible characters used to smuggle instructions past a human reader.
_ZERO_WIDTH = (
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\ufeff",  # zero-width no-break space / BOM
    "\u2060",  # word joiner
)

# Instruction-like words that make a hidden HTML/markdown comment suspicious.
_COMMENT_KEYWORDS = ("ignore", "system", "instruction", "prompt", "forget", "you are", "pretend")


_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def _triggers(content: str) -> list[str]:
    """Collect the labels of every injection pattern present in `content`, in a
    stable order. Empty list means nothing matched."""
    lowered = content.lower()
    hits: list[str] = [m for m in _MARKUP if m in lowered]
    hits += [p for p in _IMPERATIVES if p in lowered]
    if any(z in content for z in _ZERO_WIDTH):
        hits.append("zero-width-char")
    for body in _COMMENT_RE.findall(content):
        low = body.lower()
        if any(k in low for k in _COMMENT_KEYWORDS):
            hits.append("hidden-comment-instruction")
            break
    return hits


class SecurityFindingEntry(BaseModel):
    """An injection pattern matched in some ingested content (no file body)."""

    model_config = ConfigDict(frozen=True)

    type: Literal["security"] = "security"
    source: str
    triggers: list[str]


# ── exec-child findings buffer ───────────────────────────────────────────
# scan_content() findings accumulate in this process-global list while agent
# SDK code runs inside an exec child — a fresh process per execute_code, the
# only place `ava.state` is set. The child drains the list with take_findings()
# into its result envelope; the exec node re-validates each entry and injects it
# as a SECURITY system note in the same exec's messages delta, after the
# exec-result ToolMessage (the tool_use -> tool_result adjacency invariant
# forbids interleaving notes between the AIMessage and its ToolMessage).
#
# The agent host is a different shape: one process serves many agents'
# concurrent turns, so it holds no findings buffer. scan_content() drops its
# finding there (`_in_exec_turn()` is False), and a claim-side inbound scan
# hands its finding straight back to the claim node (scan_inbound_content),
# which puts the note in that claim's own messages delta.
_pending_findings: list[SecurityFindingEntry] = []


def _in_exec_turn() -> bool:
    """True when scan_content runs inside an exec child — the only place a
    finding can be delivered (there is a messages delta to inject into)."""
    import ava  # lazy: same-layer, avoids import cycle at module load

    return ava.state is not None


def _record_finding(source: str, triggers: list[str]) -> None:
    """Buffer one security finding for the exec child's result envelope. No-op
    when security scanning is disabled, or outside an exec child (no messages
    delta exists to inject into, and a host-wide buffer would be shared by
    every agent the host serves)."""
    from base.config import settings

    if not settings.agent.security_scan_enabled:
        return
    if not _in_exec_turn():
        return
    _pending_findings.append(SecurityFindingEntry(source=source, triggers=triggers))


def scan_content(content: str, source: str = "unknown") -> str:
    """Return `content` unchanged.

    When a prompt-injection pattern is present, the finding is buffered
    in-memory for the exec node to deliver as a SECURITY system note in this
    exec's messages delta. Outside an exec turn the finding is deliberately
    dropped: there is no delta to own it. Claim-side inbound construction must
    call scan_inbound_content() instead, whose caller owns the finding. The
    returned content is always clean — no warning is prepended.
    """
    hits = _triggers(content)
    if hits:
        _record_finding(source, hits)
    return content


def scan_inbound_content(content: str, source: str) -> SecurityFindingEntry | None:
    """Scan claimed inbound `content`; return its finding, or None.

    None when nothing matched or scanning is disabled. Touches no process
    state: the caller owns the finding and delivers it as a SECURITY note in
    its own messages delta, so concurrent agent turns in one host process
    cannot see each other's findings. The content itself is never altered.
    """
    from base.config import settings

    if not settings.agent.security_scan_enabled:
        return None
    hits = _triggers(content)
    return SecurityFindingEntry(source=source, triggers=hits) if hits else None


def take_findings() -> list[SecurityFindingEntry]:
    """Return all pending exec-child findings and clear the buffer.

    The exec child drains its findings into the result envelope when the run
    ends. Returns an empty list when nothing was flagged. Clearing on read
    means each finding is delivered exactly once — there is no file to
    truncate.
    """
    out = list(_pending_findings)
    _pending_findings.clear()
    return out


def is_flagged(content: str) -> bool:
    """True when `content` carries injection patterns.

    Findings are delivered as SECURITY system notes (exec-child findings by the
    exec node, inbound findings by the claim node), never as a marker inside the
    content, so this checks `_triggers` directly: does the content contain
    injection patterns?
    """
    return bool(_triggers(content))
