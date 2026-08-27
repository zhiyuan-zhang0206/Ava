"""ava_memory plugin — shared memory pool: the full `ava.memory` surface + passive recall hook + daily consolidation skill.

Three capabilities live under one plugin because they share the same domain (the
shared memory pool at ava.memory.PATH):

1. **ava.memory namespace** (PATH + search + write) — the agent-facing SDK surface.
   This plugin OWNS the `ava.memory` top-level namespace. Disabling it removes
   `ava.memory` entirely — PATH, search(), write(), IndexerUnavailable — not just
   passive recall.

2. **Passive memory recall** (before_llm hook) — on a turn woken by fresh user
   inbound, searches the pool semantically on the recent conversation and
   injects the top matching note references so relevant durable notes surface
   without the agent asking. The recall engine lives in
   agent/graph/_memory_recall.py; this plugin is the wiring plus its firing
   gates.

3. **Memory stewardship + daily consolidation** (bundled skills) — the pool is
   a git repo. Once a day the day's notes are committed, pushed, and re-indexed
   so search stays current. The procedure varies by deployment: single-box runs
   `skills/scripts/consolidate.py`; multi-host spawns one steward per machine, then
   the arbiter merges all PRs. The stewardship playbook (role, health checks,
   note merging, query service) is at ava.skills.ava_memory; the CLI-backed
   consolidation procedure is the ava.skills.ava_memory.consolidation
   sub-skill.

Disabling this plugin removes the entire ava.memory surface, passive recall,
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
rendering) lives in agent/graph/_memory_recall.py; this plugin is just the
before_llm wiring plus its firing gates.

Firing gates:
- Feature gate: no-op unless `turn_settings.agent.passive_memory_recall_enabled`.
- Trigger gate: fire when the message tail carries fresh inbound from a real
  source — user chat, a peer agent (`agent:`), a scheduled turn (`schedule:`),
  or a system notice — and skip machine-originated wake-ups (`watcher:` /
  `shell:` prefixes; `tail_has_recallable_inbound`). A silent-idle continue or a
  lifecycle marker carries no inbound at all and is skipped too. This same tail
  shape (an inbound sits after the last AIMessage) makes recall mutually
  exclusive with hooks that fire on a bare AIMessage tail (e.g. silent-idle), so
  they never contend for the write.

Same-session dedup: the injected paths accumulate in `state.memory` via the
`memory` channel's union reducer (`_memory_state_merge`); this hook passes the
accumulated set into the recall call and writes back only the fresh paths, so a
note surfaces at most once per session.

compact clobber-safety: auto-compact is also a before_llm hook. `messages`
carries the add_messages reducer, so co-writing it does NOT fail-loud — the
runner merges both hooks' values. But auto-compact's full-history REMOVE_ALL
replacement is order-sensitive and would swallow a note appended in the same
pass. So when auto-compact would fire this same turn, this hook defers
(returns None) rather than racing the history replacement; recall simply
retries on the next turn.
"""

from __future__ import annotations

__description__ = "Shared memory pool: ava.memory SDK surface (PATH + search + write) + passive recall hook (auto-surfaces relevant notes) + daily consolidation skill (commit, push, re-index)"

try:
    import fcntl
except ImportError:  # Windows ships no fcntl module; the index lock degrades (see _locked_update)
    fcntl = None  # type: ignore[assignment]
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as _SimpleNamespace

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

import ava as _ava
import shared.machine
import shared.paths
from agent.graph._context import AvaContext
from agent.graph._context_notes import (
    RANK_CLUSTER_MEMORY,
    RANK_PER_AGENT_MEMORY,
    register_context_note,
)
from agent.graph._memory_recall import passive_memory_recall
from agent.graph._system_prompt import register_system_prompt_section
from agent.hooks import Hook, register_before_llm
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import tail_has_recallable_inbound
from agent.state import AgentState, MemoryState
from ava import _gateway_client as _client
from shared.agents import IndexerUnavailable as IndexerUnavailable
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.log import logger
from shared.paths import ava_home as _ava_home

from .notes import memory_index_note, per_agent_memory_note

# The two memory indexes join the framework's ordered context-note registry, so
# `init_context` lays them down whenever a window is established. The ranks pin
# them into the reading order the framework documents (see the rank scale in
# `_context_notes.py`): the shared index right after the exec-timeout note, the
# per-agent index right after the agent-id note. Registered here rather than by
# a decorator in notes.py: this module is re-executed on every plugin (re)load,
# while importing notes.py hits the sys.modules cache — so decorators there
# would not survive a clear_plugin_registrations cycle. Disabling this plugin
# means neither is ever registered, and a window is laid down with no memory
# notes in it — matching an agent that has no memory stores.
# The shared index is NOT `on_fork`: the pool is cluster-wide, so the copy
# a fork inherits with the source's history is the same content the graft would
# add — grafting it duplicates the index in the forked agent's window
# (issue #1320). Same reasoning as the framework's timezone note. The per-agent
# index IS `on_fork`: it names the source agent's store, which the inherited
# history renders wrong for the new agent — `_handle_fork` strips the inherited
# copy before grafting the new agent's own.
register_context_note(rank=RANK_CLUSTER_MEMORY)(memory_index_note)
register_context_note(on_fork=True, rank=RANK_PER_AGENT_MEMORY)(per_agent_memory_note)

_MEMORY_DOC = """Long-term notes as markdown files, with semantic search to find them.

Two stores:

- Shared pool (`ava.memory.PATH`): notes visible to every agent. Restrained:
  reusable rules, repeatedly-referenced facts, and user rulings only; events
  stay out by default (git history already carries them).
- Per-agent memory (`<workspace>/memory/`): your own durable state.
  `MEMORY.md` there is the index — one line per memory, injected into your
  context at cold start and after each compact; each memory is one file
  beside it, read on demand. `write(slug, content, ..., store="personal")`
  writes the entry and maintains its index.

Use `write(slug, content, ..., store="personal")` to write either store; it
resolves an absolute store-owned path, so it is immune to `ava.cwd` changes.
"""

_PATH_DOC = """When the pool spans machines, notes sync within about a day — a path `search`
returns may not have arrived here yet; retry later.

Start each note with YAML frontmatter, then the attribution header:

    ---
    type: Memory
    ava_agent: <your id>
    ---
    <!-- agent-<your id> @ <your machine>, YYYY-MM-DD HH:MM -->

Use `write(slug, content, ..., store="shared")` as the canonical writer:
an absolute pool path, immune to `ava.cwd` changes."""

_memory_ns = _SimpleNamespace()
_memory_ns.__doc__ = _MEMORY_DOC
_memory_ns.PATH = _ava.const(_ava_home() / "memory", doc=_PATH_DOC)
_memory_ns.IndexerUnavailable = IndexerUnavailable


def _search(query: str, k: int = 5) -> list[tuple[Path, str, list[str]]]:
    """Semantic search; return the most relevant notes as (absolute path,
    description, tags) tuples. The description is "" when absent; tags carry
    the note's `type/<x>` tag."""
    results = _client.memory_search(query, k)
    return [(_memory_ns.PATH / r.path, r.description, list(r.tags)) for r in results]


_memory_ns.search = _search

_PERSONAL_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _entry_path(slug: str, store: str, agent_id: int) -> tuple[Path, bool]:
    """Resolve a memory entry to its store-owned absolute path.

    Shared entries may use topic directories, but neither store accepts a path
    that can escape its root. Personal names are intentionally narrower because
    that index is the agent's stable, flat namespace.
    """
    if not slug or slug.endswith(".md"):
        raise ValueError("memory slug must be a non-empty filename without .md")
    relative = Path(slug)
    if (
        relative.is_absolute()
        or "\\" in slug
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise ValueError("memory slug must be a relative path inside its store")
    if store == "personal":
        if not _PERSONAL_SLUG_RE.fullmatch(slug):
            raise ValueError("personal memory slug must be one kebab-case name without slashes")
        return shared.paths.workspace_dir(agent_id) / "memory" / f"{slug}.md", False
    if store == "shared":
        if relative == Path("MEMORY"):
            raise ValueError("shared memory slug cannot replace MEMORY.md")
        return shared.paths.memory_dir() / relative.with_suffix(".md"), True
    raise ValueError("memory store must be 'personal' or 'shared'")


def _validated_tags(tags: list[str] | None) -> list[str]:
    """Return tags after enforcing the one-type-tag memory invariant."""
    values = ["type/reference"] if tags is None else list(tags)
    if sum(tag.startswith("type/") and len(tag) > len("type/") for tag in values) != 1:
        raise ValueError("memory tags must contain exactly one type/<x> tag")
    return values


def _write_atomically(path: Path, content: str) -> None:
    """Replace one memory entry without exposing a partially written note."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(temporary, path)  # noqa: PTH105 — atomic publication required by the memory contract
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


_INDEX_DESCRIPTION_MAX = 120
"""Cap on the description text rendered into a MEMORY.md pointer line.

The index is injected into agent context at cold start and after each compact,
so an unbounded description would leak into every agent's context budget — the
one unbounded input on this path. The note itself keeps the full text; only
the index line is truncated."""


def _pointer_line(title: str, relative_path: str, description: str) -> str:
    """Render the one-line index entry for a durable memory note."""
    if len(description) > _INDEX_DESCRIPTION_MAX:
        description = description[:_INDEX_DESCRIPTION_MAX].rstrip() + "..."
    return f"- [{title}]({relative_path}) — {description or title}"


def _locked_update(index_path: Path, update: Callable[[str], str]) -> None:
    """Apply one index update while holding its advisory per-file lock.

    POSIX: fcntl.flock, unchanged. Windows has no fcntl module, so the lock
    degrades to a no-op there — the update itself still runs, unguarded. The
    lock is advisory and each upsert rewrites a single pointer line in place,
    so an unlocked Windows update can at worst drop one line under a
    concurrent writer; on a single-user box that beats crashing every agent at
    plugin load (unguarded import introduced by 6e96b1554)."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a+", encoding="utf-8") as index_file:
        if fcntl is not None:
            fcntl.flock(index_file.fileno(), fcntl.LOCK_EX)
        try:
            index_file.seek(0)
            text = update(index_file.read())
            index_file.seek(0)
            index_file.truncate()
            index_file.write(text)
            index_file.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(index_file.fileno(), fcntl.LOCK_UN)


def _upsert_index(
    root: Path, relative_path: str, title: str, description: str, *, shared: bool
) -> None:
    """Replace or append one index pointer without disturbing other entries."""
    index_path = root / "MEMORY.md"
    pointer = _pointer_line(title, relative_path, description)
    target = re.compile(rf"^- \[[^]]+\]\({re.escape(relative_path)}\) — .*$")

    def update(text: str) -> str:
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if target.fullmatch(line)]
        if matches:
            lines[matches[0]] = pointer
            for index in reversed(matches[1:]):
                del lines[index]
        elif shared and "## Pointers" in lines:
            section_start = lines.index("## Pointers")
            section_end = next(
                (
                    index
                    for index in range(section_start + 1, len(lines))
                    if lines[index].startswith("## ")
                ),
                len(lines),
            )
            pointer_lines = [
                index
                for index in range(section_start + 1, section_end)
                if lines[index].startswith("- [")
            ]
            lines.insert(pointer_lines[-1] + 1 if pointer_lines else section_start + 1, pointer)
        else:
            lines.append(pointer)
        return "\n".join(lines) + "\n"

    _locked_update(index_path, update)


def write(
    slug: str,
    content: str,
    *,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    store: str = "personal",
) -> Path:
    """Upsert the store's MEMORY.md pointer.

    Personal entries use a flat kebab-case name in the calling agent's
    workspace; shared entries may use topic directories in the memory pool.
    Both targets are absolute store paths.

    Each index update holds an advisory lock on the store's `MEMORY.md`, so
    concurrent writers in this and other processes are serialized.
    """
    from ava._boot import _agent_id

    agent_id = _agent_id
    if agent_id is None:
        raise RuntimeError("ava.memory.write requires an established agent id")
    entry, is_shared = _entry_path(slug, store, agent_id)
    values = _validated_tags(tags)
    note_title = title or slug
    note_description = description or ""
    if is_shared:
        now = datetime.now(UTC)
        machine = shared.machine.machine_name()
        frontmatter = (
            f"---\ntype: Memory\nava_agent: {agent_id}\ntitle: {note_title}\n"
            f"description: {note_description}\ntags: [{', '.join(values)}]\n"
            f"timestamp: '{now.isoformat()}'\nava_machine: {machine}\n---\n"
            f"<!-- agent-{agent_id} @ {machine}, {now:%Y-%m-%d %H:%M} -->\n\n"
        )
    else:
        frontmatter = (
            f"---\nname: {slug}\ndescription: {note_description}\n"
            f"tags: [{', '.join(values)}]\n---\n\n"
        )
    _write_atomically(entry, frontmatter + content)
    root = (
        shared.paths.memory_dir() if is_shared else shared.paths.workspace_dir(agent_id) / "memory"
    )
    _upsert_index(
        root, entry.relative_to(root).as_posix(), note_title, note_description, shared=is_shared
    )
    return entry.resolve()


_memory_ns.write = write

# __all_for_ava__: the curated agent-facing surface. IndexerUnavailable is
# intentionally excluded — it is reachable (ava.memory.IndexerUnavailable)
# but does not appear in help(ava.memory).
_memory_ns.__all_for_ava__ = ["PATH", "search", "write"]

_ava.register_namespace("memory", _memory_ns)

# ── Memory discipline (system prompt section) ──────────────────────────

# The write discipline both stores share. It is fixed text — the same bytes for
# every agent on every turn — so it belongs in the system prompt, not in a note
# beside the indexes: an index carries content that changes as the agent writes,
# this does not.
_DISCIPLINE = """\
# Remembering across sessions

Your memory — two stores, one discipline.

- Shared pool (ava.memory.PATH): notes every agent can read. Restrained by
  design: only reusable rules (each with a Why and a How to apply), facts many
  agents repeatedly reach for, and user rulings. Events stay out by default —
  git history already carries them. Judge each note by one question: "would a
  new agent reading this in a year behave differently?" If not, it does not
  belong in the pool.
- Personal (memory/ in your workspace): your own durable state, maintained by
  you alone. Verbose is fine here: process details, half-formed understanding,
  notes of uncertain future value — keep them personal until they earn a place
  in the pool.

Unsure which store? Personal — the pool is for rules other agents will use,
not your working details.

Each store puts its index in front of you when a session starts and after a
compact; the entries themselves you read on demand.

What makes a memory worth writing — applicable, durable, legible:

- applicable: it changes what you do later — a correction you were given, a
  stated preference, a non-obvious procedure or invariant. Not what the code,
  repo docs, AGENTS.md, or a fresh lookup already tells you.
- durable: it holds in more than one session, written as a reusable rule ("this
  service rate-limits above 3 retries, so more retries make it worse"), not as
  an episode ("set retries to 3 here"). Live status ("in flight", "awaiting
  review"), branch and PR inventories, and who currently holds which role are
  cluster state, not memory.
- legible: readable months later by an agent that was not there — one topic per
  file, full sentences, the why and not only the what. No bare ticket ids, no
  "the fix".

You must write or update memory when:

- Someone corrects you, however it is phrased: a "do it this way instead" edit,
  a pushback, or a skeptical question ("won't that break X?"). Answer it, then
  record the preference behind it — answering is not saving. Scope words ("for
  now", "in this change") mark a one-off to follow, not a rule to save.
- You learn something durable about your environment: a path, a port, a quirk of
  this machine or cluster that a fresh lookup would not reveal.

Write it in the same turn you engage the correction, before you treat that turn
as finished. An offered next step is a finished engagement, not permission to
defer.

Memory maintenance is an important standing duty — not a side chore you
fit in after your real task. When you notice a memory that is stale or wrong,
update it first, before continuing whatever you were doing. Don't "notice and
move on", and don't wait for consolidation to sweep it up; a stale note keeps
misleading every later agent until it is corrected.

Correcting stale memory is an obligation, not an option:

- When a memory — yours, a peer's, or the shared index's Setup section —
  contradicts a self-verifying fact (a branch that no longer exists, a path
  that moved, a setting that changed), fix it in the same turn you notice it:
  edit the entry in place. Appending a newer note beside the stale claim
  leaves the contradiction in front of every agent; only the correction
  removes it.
- The shared index's Setup section (repos, key facts, current tasks) has no
  single owner. Whoever spots an outdated claim there is responsible for
  correcting it — or reporting it to the Memory Arbiter when unsure.
- Not sure how to fix it? Ask the owning agent — but uncertainty is not an
  excuse to leave it alone: at minimum mark it "possibly stale — needs
  confirmation" so later readers do not trust it.
- Before writing, search first: check the index and the pool for an entry
  that already covers the ground. Update it if it exists; create only when
  nothing does. Deleting an entry that turns out to be wrong is a valid write.

Every note carries exactly one type tag: type/user (who the user is),
type/feedback (how you should work, with the reason), type/project (ongoing
work, goals, constraints), type/reference (pointers to external resources),
type/env (machine and cluster facts), type/role (an agent's role and its
boundaries). Lead a type/feedback or type/project body with the rule or fact,
then a "Why:" line and a "How to apply:" line.

A memory is a claim, not a source of truth. Your sources differ in how they can
be checked:

- self-verifying: running code, ava cluster status, the database, the
  filesystem. Read it and you know the current answer.
- asserted: AGENTS.md, conventions/, decisions/, okf/*.ava.okf.md.
  Maintained and reviewed, but they can lag the code.
- remembered: the shared pool and your own memory. A snapshot, carrying an
  author and a timestamp.

When two disagree, work out which one is current rather than following a fixed
ranking: check the self-verifying source when there is one, and weigh timestamps
and authorship when there is not. A memory the code contradicts is usually
stale — fix it at the source. A checked-in doc the code contradicts is worth
reporting, not silently working around."""


@register_system_prompt_section
def memory_discipline_section() -> str:
    """Toggle via settings.agent.prompt_memory_behavior_enabled (env
    AVA_SYSTEM_PROMPT_MEMORY, default on). Empty when both stores are switched
    off — with nothing to write to, the discipline would describe a capability
    the agent does not have. The mechanics of reading/writing/searching live in
    the ava.memory SDK docstrings and the two index notes, so this section
    deliberately does not repeat them."""
    from shared.lm.registry import resolve_setting

    if not resolve_setting("prompt_memory_behavior_enabled", model=turn_settings.lm.llm_model):
        return ""
    if not (
        settings.agent.memory_index_inject_enabled or settings.agent.memory_per_agent_inject_enabled
    ):
        return ""
    return _DISCIPLINE


# ── Passive memory recall hook ─────────────────────────────────────────


class _PassiveMemoryRecallHook(Hook):
    """Before an LLM turn, search the memory pool on the recent conversation and
    inject fresh top matches as a note.

    No-op (returns None) when the feature is disabled, when the tail has no
    recallable inbound (only a watcher or shell wake-up), when auto-compact
    would replace messages this same turn
    (deferring avoids two before_llm hooks writing `messages` in one pass), or
    when recall finds nothing new to add.
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
        if not turn_settings.agent.passive_memory_recall_enabled:
            return None

        if not tail_has_recallable_inbound(state.messages):
            return None

        if auto_compact_will_fire(state):
            logger.info(
                "[{label}] {body}",
                label="passive-recall",
                body="defer: auto-compact predicted, skipping recall this turn",
                event="passive_recall",
            )
            return None

        recall = await passive_memory_recall(
            state.messages, injected_paths=state.memory.injected_paths
        )
        if recall is None:
            return None

        # Write only the fresh paths; the `memory` channel's union reducer
        # accumulates them onto the current set.
        return {
            "messages": [recall.note],
            "memory": MemoryState(injected_paths=recall.paths),
        }


passive_memory_recall_before_llm = _PassiveMemoryRecallHook()
register_before_llm(passive_memory_recall_before_llm)
