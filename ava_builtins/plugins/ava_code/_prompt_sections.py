"""The ava_code plugin's system-prompt sections: coding-tool advertising and the opt-in
debugging workflow. Kept apart from `agent_runtime.py` (the host-side face), which does not
import `ava`: the coding-tools section renders SDK stubs through `ava.help`.
"""

from __future__ import annotations

import contextlib
import io

import ava
from base.host.env.agent_slices import AgentSlices

# ── system prompt section: coding tool advertising ──────────────────────────
# From the plugin's perspective, what "hands" should the agent use for
# coding? Feed cwd / files / shell as three modules into help() — the
# renderer is Python-stub-format, each module renders the full docstring +
# each child function as `def name(sig): """doc"""`; the agent gets the
# full contract with zero drill-down.

_PROMOTED_MODULES = ("cwd", "files", "shell")


def _coding_tools_section(slices: AgentSlices) -> str:
    """Render the cwd / files / shell modules as Python stubs under `## ava.X`.

    A module already expanded by the framework's "Expanded SDK reference"
    section (the effective list: plugin registrations + AVA_SDK_EXPAND, exact
    path match) is skipped here — its full contract is in the prompt once
    already; the preamble conventions below still apply and are always
    rendered. With the default config every promoted module is expanded, so
    this section reduces to the preamble."""
    from agent.graph.prompt.system_prompt import effective_sdk_expand

    expanded = set(effective_sdk_expand(slices.prompt.sdk_disable))
    pieces: list[str] = []
    for name in _PROMOTED_MODULES:
        if name in expanded:
            continue
        mod = getattr(ava, name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ava.help(mod)
        pieces.append(buf.getvalue().rstrip())
    body = "\n\n".join(pieces)
    preamble = (
        "Use the Ava file and shell tools for coding work. "
        "When you start a coding task:\n\n"
        "- Point your working directory at the project root once when you start.\n"
        "- Read `AGENTS.md` first (via the file tools) before reading other "
        "code — it sets the project rules you must follow.\n"
        "- Search with `rg` (ripgrep), not `grep -r`/`find` — recursive grep scans "
        "every `.worktrees/` checkout and often hits the 30s shell timeout.\n"
        "- For any change to a repo, work in an isolated `git worktree` — never edit, "
        "switch the branch of, or push the shared checkout directly; every change goes "
        "through a PR.\n"
        "- Every commit appends a `Co-authored-by: Ava #<your agent id>` trailer; PR "
        "titles start with `[Ava-<your agent id>]`.\n"
        "- Before calling a change done, run the narrowest check that proves it (the "
        "failing test, the exact command), then widen to nearby cases — don't claim "
        "success from reading the diff alone.\n"
        "- `execute_code` has a hard timeout (default 300s); keep it to short, quick "
        "work. Run anything long-running (e.g. `time.sleep`, large downloads, long "
        "polling) in an `ava.shell.sessions` persistent shell instead."
    )
    if not body:
        return f"# Coding tools\n\n{preamble}"
    return f"# Coding tools\n\n{preamble}\n\n{body}"


# ── system prompt section: debugging workflow (opt-in) ──────────────────────
# Coding-specific advice, so it belongs to this plugin rather than the core
# prompt. Off by default; toggled by adding "ava_code_workflow" to
# settings.agent.system_prompt_extra (env AVA_SYSTEM_PROMPT_EXTRA).
# Empty return when disabled = no contribution.
def _engineering_workflow_section(slices: AgentSlices) -> str:
    """Loose bug-fix-workflow advice, gated by system_prompt_extra=ava_code_workflow."""
    if "ava_code_workflow" not in slices.prompt.system_prompt_extra:
        return ""
    return (
        "## Resolving issues and debugging\n\n"
        "When you're trying to resolve an issue or chase down a bug, start by "
        "reproducing the failure. That's the foundation — without a reliable "
        "repro you're guessing at causes.\n\n"
        "Once you have it, push further. The failing case in front of you is "
        "one way the bug shows up; the same underlying defect usually has "
        "several other manifestations. As you reproduce, keep asking: what "
        "other inputs hit this code path? What adjacent edge cases would also "
        "fail? The point is to map the space of failures, not confirm a "
        "single instance.\n\n"
        "Reproduction and root-cause analysis feed each other. Each step "
        "deeper into the cause reveals new failing inputs; each new failing "
        "input sharpens the cause. Treat them as iteration, not two "
        "sequential phases.\n\n"
        "Before believing your fix is done, run it against the variations "
        "you've discovered, not just the original case. A fix that handles "
        "one instance but doesn't address the broader pattern is usually the "
        "wrong fix.\n"
    )
