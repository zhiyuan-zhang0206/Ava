"""ava_sdk_reminder state schema + detection tables — kept side-effect-free so
tests can import the schema and the matchers without triggering hook
registration (importing `plugin.py` calls `register_plugin_state` + the hook
registrations).

Three reminder families share one `reminded` set:
- Four code-cell categories (shell/wait/files/http): a regex scan over the
  executed code (string/comment/f-string literal spans masked before matching),
  checked in the after_exec hook.
- Assumed-persistence NameErrors: the after_exec hook records one dynamic key
  per undefined name that appeared in an earlier code cell.
- One inbound category (agent_reply): scan the message tail for an inbound
  from another agent, checked in the before_llm hook (the agent may reply in
  plain text without ever running code, so the reminder must land before the
  reply is produced).

`reminded` tracks once-scoped categories and NameError names already hinted;
`last_seen_compact` bookmarks the compact.version monotonic counter so the set
re-arms (clears) after a compaction strips the messages that carried an earlier
hint — the same lazy version-counter reset `ava_code` applies to its
`injected_paths`.
"""

from __future__ import annotations

import io
import re
import tokenize

from pydantic import BaseModel, Field


class AvaSdkReminderState(BaseModel):
    """plugins.ava_sdk_reminder persistent state.

    - reminded: once-scoped categories and dynamic NameError-name keys already
      hinted this context window. Cleared (re-armed) when a compaction advances
      the version counter. No reducer (last-value): this plugin's two hooks
      (after_exec for code categories and NameErrors, before_llm for
      agent_reply) are the only writers, never write in the same node run, and
      always commit the full new set.
    - last_seen_compact: bookmark compared against the compact version
      counter; when it advances, the earlier hint lines have been summarized
      away, so reminded is cleared and the bookmark catches up.
    """

    reminded: set[str] = Field(default_factory=set)
    last_seen_compact: int = 0


# ── detection categories ───────────────────────────────────────────────────
#
# Two parallel dicts keyed by the same category strings: `_TRIGGERS` maps each
# category to its compiled trigger regex, `_HINTS` maps it to the hint line
# surfaced as a system note. The regexes deliberately under-match rather
# than over-match — a missed native call just means no hint that round (the
# agent's code still ran), whereas a false positive nags on legitimate
# SDK-adjacent usage. Emission order when a single cell hits multiple
# categories is the authoritative `CATEGORIES` tuple below (not these dicts'
# insertion order).
#
# Two shared guardrails apply before the regexes run (see `detect_categories`):
# - literal masking: string/comment/f-string literal spans are blanked before
#   matching, so trigger words that appear only inside literals (grep
#   patterns, printed examples, docstrings) never fire a hint.
# - the `files` category fires only for file-CONTENT read/write bypasses
#   (`open()`, pathlib read/write methods, shutil content ops). Listing and
#   management calls — glob.glob, os.listdir, os.remove, os.unlink,
#   os.makedirs — do not trigger: an agent that lists names with stdlib while
#   reading content through ava.files has not bypassed the SDK (user ruling
#   2026-08-26). ava.files.glob itself is untouched.

_TRIGGERS: dict[str, re.Pattern[str]] = {
    "shell": re.compile(r"\bsubprocess\.|\bos\.system\(|\bos\.popen\("),
    "wait": re.compile(r"\btime\.sleep\(|(?<![.\w])sleep\("),
    "files": re.compile(
        r"(?<![.\w])open\(|"
        r"\.(?:read_text|write_text|read_bytes|write_bytes)\(|"
        r"\bshutil\.(?:copy|copy2|copyfile|copytree|move|rmtree)\("
    ),
    "http": re.compile(r"\brequests\.\w+\(|\bhttpx\.|\burllib\.request\b"),
}

# Hint lines are surfaced verbatim as a system note the agent reads. English
# only; framed as "there is a smoother primitive", not a prohibition; each
# points at a self-serve help() entry.
_HINTS: dict[str, str] = {
    "shell": (
        "For running shell commands there is a handier primitive: "
        "`ava.shell.run(...)`. See `help(ava.shell)`."
    ),
    "wait": (
        "Instead of a timed wait or sleep loop there is a handier "
        "primitive for waiting on a condition: `ava.watcher`. See "
        "`help(ava.watcher)`."
    ),
    "files": (
        "For reading, writing, and managing files there is a "
        "handier primitive: `ava.files`. See `help(ava.files)`."
    ),
    "http": (
        "For fetching web content there is a handier primitive: `ava.web`. See `help(ava.web)`."
    ),
}

# Stable emission order so a multi-category cell lists hints predictably.
CATEGORIES: tuple[str, ...] = ("shell", "wait", "files", "http")


# Token types whose text is literal, never executed code: strings (incl.
# docstrings), comments, and the literal parts of f-strings (Python 3.12
# tokenizes an f-string's literal segments as FSTRING_START/MIDDLE/END; the
# {expressions} between them stay real tokens and are NOT masked).
_LITERAL_TOKEN_TYPES = (
    tokenize.STRING,
    tokenize.COMMENT,
    tokenize.FSTRING_START,
    tokenize.FSTRING_MIDDLE,
    tokenize.FSTRING_END,
)


def _mask_literals(code: str) -> str:
    """Blank literal-token spans with spaces, preserving line layout.

    The category regexes run against the masked text so trigger words inside
    strings/comments/f-string literal parts cannot fire a hint. On tokenize
    failure (broken code) the cell is returned unmasked — the raw-scan
    behavior is the conservative fallback for code that cannot parse anyway.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except Exception:
        return code
    lines = code.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    chars = list(code)
    for tok in tokens:
        if tok.type not in _LITERAL_TOKEN_TYPES:
            continue
        start = offsets[tok.start[0] - 1] + tok.start[1]
        end = offsets[tok.end[0] - 1] + tok.end[1]
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def detect_categories(code: str) -> list[str]:
    """Return the categories whose trigger regex matches `code`, in CATEGORIES
    order. Empty list = no native-Python idiom worth hinting. Literal spans
    (strings/comments/f-string literal parts) are masked before matching.
    """
    masked = _mask_literals(code)
    return [cat for cat in CATEGORIES if _TRIGGERS[cat].search(masked)]


def hint_for(category: str) -> str:
    """The one-line hint surfaced as a system note for a category."""
    return _HINTS[category]


def mentions_watcher(code: str) -> bool:
    """Whether the cell already references `watcher`.

    A cell that trips the "wait" trigger while also naming `watcher` is the
    agent working with the watcher primitive itself (writing or testing watcher
    code that sleeps). Pointing it back at `ava.watcher` would be noise, so the
    after_exec hook marks the wait category seen without emitting its hint.
    Detection lives here beside the trigger tables; the mark-without-emit is
    applied in the hook.
    """
    return "watcher" in code.lower()


# ── the inbound (agent_reply) category ─────────────────────────────────────
#
# This category is triggered by the conversation, not by executed code: when a
# message from another agent arrives, the agent tends to answer in plain text,
# which the other agent never sees. The hint points at the delivery primitive.
# Kept separate from CATEGORIES (which is the code-cell after_exec set) so the
# two hooks stay independent while sharing the same `reminded` set.
AGENT_REPLY_CATEGORY = "agent_reply"

AGENT_REPLY_HINT = (
    "This message came from another agent. A plain text reply is "
    "not delivered to other agents; to respond, call "
    "`ava.agents.send_message(agent_id, content)`. See `help(ava.agents)`."
)

# tail_has_agent_inbound moved to agent/messages.py (the read-side counterpart
# to inbound_message); ava_compact's compact reminder also needs it to defer to
# the agent-reply note, so it lives in the framework, not this plugin.
