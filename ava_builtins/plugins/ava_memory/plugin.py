"""ava_memory plugin — shared memory pool: the full `ava.memory` surface + passive recall hook + write check + daily consolidation skill.

Four capabilities live under one plugin because they share the same domain (the
shared memory pool at ava.memory.PATH):

1. **ava.memory namespace** (PATH + PERSONAL_PATH + search) — the agent-facing SDK surface.
   This plugin OWNS the `ava.memory` top-level namespace. Disabling it removes
   `ava.memory` entirely — PATH, PERSONAL_PATH, search(), IndexerUnavailable — not just
   passive recall. There is no writer: an agent writes a note with `ava.files.write` under
   PATH or PERSONAL_PATH.

2. **Passive memory recall** (before_llm hook) — on a turn woken by fresh user
   inbound, searches the pool semantically on the recent conversation and
   injects the top matching note references so relevant durable notes surface
   without the agent asking. The recall engine lives in
   agent/graph/recall/memory_recall.py; this plugin is the wiring plus its firing
   gates.

3. **Memory stewardship + daily consolidation** (bundled skills) — the pool is
   a git repo. Once a day the day's notes are committed, pushed, and re-indexed
   so search stays current. The procedure varies by deployment: single-box runs
   `skills/scripts/consolidate.py`; multi-host spawns one steward per machine, then
   the arbiter merges all PRs. The stewardship playbook (role, health checks,
   note merging, query service) is at ava.skills.ava_memory; the CLI-backed
   consolidation procedure is the ava.skills.ava_memory.consolidation
   sub-skill.

4. **Memory write check** — wraps `ava.files.write` / `append` / `edit` to record which memory
   notes (.md files under PATH or PERSONAL_PATH) an exec call wrote, and an after_exec hook
   (`agent_runtime.py`, logic in `write_checks.py`) that checks them in the agent process: the
   pool's own single-note rules (`template/validate.py`) for every note, and for shared notes a
   duplicate search whose probabilities a decision model produces. The outcome only ever adds a
   reminder note: the write is never blocked, rewritten or made to raise, and a check that fails or
   runs out of time is logged and dropped. The wraps are the only part inside the exec subprocess
   (`written_notes.py`): no network request and no httpx or decision-model import.

   Wrap order: plugins stack in name order and the later one is outermost, so `ava_memory` sits
   outside `ava_code`'s cwd-tracking wraps of the same two targets. It calls the layer below
   exactly once, hands back its result (or lets its exception propagate), and only then records
   the path; because it sees the caller's raw path it resolves a relative one against the
   tracked `ava.cwd` itself.

Disabling this plugin removes the entire ava.memory surface, passive recall, the write check,
and the bundled memory skills.

Passive recall in detail:

Where the standing memory index keeps MEMORY.md permanently in front of the
agent, passive recall reaches into the *rest* of the pool: on a turn woken by
fresh inbound, it runs a semantic search keyed on the recent conversation
and injects the top matches (path + frontmatter description, the same fields
ava.memory.search returns) as a system-styled note. The agent sees durable
notes relevant to what is being said without having to call ava.memory.search
itself.

The heavy lifting (query build, search, dedup vs already-injected paths, note
rendering) lives in agent/graph/recall/memory_recall.py; this plugin is just the
before_llm wiring plus its firing gates.

Firing gates:
- Feature gate: no-op unless `passive_memory_recall_enabled`.
- Trigger gate: fire when the message tail carries fresh inbound from a real
  source — user chat, a peer agent (`agent:`), a scheduled turn (`schedule:`),
  or a system notice — and skip machine-originated wake-ups (`watcher:` /
  `shell:` prefixes; `tail_has_recallable_inbound`). A silent-idle continue or a
  lifecycle marker carries no inbound at all and is skipped too. This same tail
  shape (an inbound sits after the last AIMessage) makes recall mutually
  exclusive with hooks that fire on a bare AIMessage tail (e.g. silent-idle), so
  they never contend for the write.

Same-window dedup: the injected paths accumulate in `state.memory` via the
`memory` channel's reducer (`memory_state_merge`); this hook passes the
accumulated set into the recall call and writes back only the fresh paths, so a
note surfaces at most once per context window. A compaction summarizes the
injected notes away, so the record belongs to one window (`MemoryState.window`
is the `compact.version` it was written under) and the hook replaces it with an
empty one when the version moves on.

compact clobber-safety: auto-compact is also a before_llm hook. `messages`
carries the add_messages reducer, so co-writing it does NOT fail-loud — the
runner merges both hooks' values. But auto-compact's full-history REMOVE_ALL
replacement is order-sensitive and would swallow a note appended in the same
pass. So when auto-compact would fire this same turn, this hook defers
(returns None) rather than racing the history replacement; recall simply
retries on the next turn.
"""

from __future__ import annotations

__description__ = "Shared memory pool: ava.memory SDK surface (PATH + PERSONAL_PATH + search) + passive recall hook (auto-surfaces relevant notes) + write check (rule and duplicate reminders for notes written with ava.files) + daily consolidation skill (commit, push, re-index)"

# This module is the plugin's SDK **surface** — the only face an agent-launched
# child loads (task #3633). Its agent-runtime registrations (the two context-note
# indexes, the memory-discipline prompt section, the passive-recall before_llm
# hook, the write-check hook) live in `agent_runtime.py`, imported only on the full path (see
# `agent/extensions/__init__.py`). The two `files.*` wraps below run in the exec
# subprocess and keep to the surface-only imports.


from collections.abc import Callable
from pathlib import Path

from base.packages.plugins.extensions import PluginContributions, SdkNamespace, SdkWrap

from . import sdk as _memory_sdk
from .written_notes import record_written_note

# ── ava.memory namespace declaration ──────────────────────────────────
# The SDK surface (PATH / PERSONAL_PATH / search) lives in `sdk.py` — a real module
# that IS the `ava.memory` namespace. the SDK install puts the same object
# on the package (attribute access) and in sys.modules under `ava.memory`
# (`import ava.memory`), so both spellings resolve identically.


# ── wrap ava.files.write / append / edit ─────────────────────────────
# Transparent: `inner` is called exactly once with the caller's arguments, its result is returned
# and its exception propagates untouched; the note is recorded only after a write that succeeded.
# Recording never raises (`record_written_note`). None of the wraps carries a docstring, so the rendered
# SDK keeps the original `files.*` contracts.
def wrapped_write(inner: Callable[..., None], path: str | Path, content: str) -> None:
    result = inner(path, content)
    record_written_note(path)
    return result


def wrapped_append(inner: Callable[..., None], path: str | Path, content: str) -> None:
    result = inner(path, content)
    record_written_note(path)
    return result


def wrapped_edit(
    inner: Callable[..., None], path: str | Path, old: str, new: str, *, replace_all: bool = False
) -> None:
    result = inner(path, old, new, replace_all=replace_all)
    record_written_note(path)
    return result


def contribute() -> PluginContributions:
    """What this plugin declares for the SDK surface."""
    return PluginContributions(
        sdk_namespaces=(SdkNamespace("memory", _memory_sdk, expand=False),),
        sdk_wraps=(
            SdkWrap("files.write", wrapped_write),
            SdkWrap("files.append", wrapped_append),
            SdkWrap("files.edit", wrapped_edit),
        ),
    )
