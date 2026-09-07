# SDK docstring discipline (audience = agent, not developer)

Three hard rules: all English, no impl-detail leak, no Markdown wrapping. This
doc is the full spec: coverage scope, what counts as an impl-detail leak, the
`help()` rendering model, and the per-section writing rules. Enforced by
`scripts/lint_agent_docstrings.py` in pre-commit.

**Scope in one sentence**: any text the agent can see falls under this section.
What the agent can't see, write however you want.

Specific coverage (**all mandatory**):

- module / class / public function docstrings in `ava/*.py`
- module / function / class docstrings in namespaces registered by plugins via `register_namespace(name, module)`
  (plugin `_*.py` private modules count too — as long as the namespace exports them to the agent, they're covered)
- docstrings of callables hung on an existing namespace via `register_namespace_member(namespace, name, fn)`
  (e.g. `ava.self.set_label`) — equally agent-facing, rendered under `help(ava.<namespace>)`
- strings returned by plugins via `register_system_prompt_section(fn)` (including stdout captured from
  `ava.help(...)` calls inside `fn` — if upstream docstrings are sloppy, they bring violations into the prompt)
- docstrings of wraps / overrides (`ava.files.read = _wrapped_read` style replacements of original SDK
  functions: the wrap function's docstring carries **exactly the same contract**: English,
  contract not "what I did to the original function")

Three hard rules:

1. **All English** — the audience is the LLM. CJK characters (including punctuation: em dash, fullwidth comma, fullwidth period, corner brackets)
   are entirely forbidden. A docstring is prompt text, not a dev note.
2. **Don't expose internal implementation** — the agent neither needs nor should know how the system is built.
   Exposing = adding prompt noise + letting the agent make decisions based on a wrong mental model.
3. **No Markdown wrapping** — a docstring is plain Python prose, not Markdown. `**bold**` emphasis is
   forbidden (enforced by the lint); single backticks around code identifiers are the one accepted
   convention. Prefer reST-free plain style throughout (no ``double backticks``).

## What implementation details actually look like (rewrite if you see any)

Sorted by frequency:

- **System component names**: `LangGraph state field` / `state_handle` / `ava.state` /
  `checkpoint` / `reducer` / `pg_notify` / `session` / `worker thread` /
  `INSERT inbound` / `envelope wrap`
- **Deployment topology / infra roles**: `gateway` / `agent-runner` / `gateway` /
  `cluster` / `runner` / `host` — the agent's world is just "machines" (places it runs,
  spawns onto, and reads a `description` for); how the fleet is deployed is not something
  it reasons over. Say "machine(s)". (The operator UI still sees roles via `/api/status`;
  the agent's `ava.agents.list_machines()` shows only the machines that run agents.)
- **History pointers**: `PR #191` / `since 2026-05-12 changed to` /
  `legacy: was X, then changed to Y`
- **Design internals**: `internal read-modify-write, not atomic` / `wrap of X` / `passes through original read` /
  `single wrapper invariant` / `singleton` / `lazy init`
- **Platform / protocol details**: `POSIX O_APPEND` / `inconsistent across OS` / `CPython 3.12` /
  `epoll` / `signal handler not reentrant`
- **Ops / multi-agent coordination**: `multi-agent coordination via cron staggered scheduling` /
  `housekeeper and inbox sweep don't overlap` / `cleanup trap` / `pid file`
- **Dev slogans**: `fail loud not silent` / `fail-fast guard` / `defensive guard`
- **Setup reminders**: `requires AVA_X env` / `must be called inside turn` —
  on violation, `raise` to say so yourself, don't repeat in the docstring

The correct angle is "the system transparently does X" — e.g. `read()` describes "reads a file, returns
its utf-8 text", **not** "calls `Path.read_text(encoding='utf-8')` and
intercepts via wrapper". The agent calls read, gets a string, on failure gets an exception — that's enough.

The dev-perspective "why it's implemented this way" stays in **a `#` comment at the top of the module** or a standalone history doc,
not in the docstring.

## Enforcement (mechanism backstop, don't rely on memory)

Writing rules alone doesn't work; rely on mechanism:

- `scripts/lint_agent_docstrings.py` runs in pre-commit — scans
  `ava/*.py` and `plugins/*/*.py` for module / public function /
  `register_namespace`-bound module / `register_system_prompt_section`-
  return-string-producer; matching CJK characters (`[\u4e00-\u9fff]`) or known impl-detail
  keywords (`state_handle` / `LangGraph` / `POSIX` / `PR #...` etc.)
  fails immediately. When a new violation pattern is found, **add it to the lint blacklist** so the discipline accumulates.
- After modifying any agent-visible docstring, locally run `build_system_prompt()` once and
  eyeball-scan the dumped output — pre-commit lint is a backstop, not a substitute for review;
  violations may be new patterns the lint hasn't caught yet.

A module's agent-visible surface is its **`__all_for_ava__`** list (a whitelist;
absent it, non-underscore public names). This is a distinct name from Python's
`__all__` — agent-surface modules declare `__all_for_ava__` and **do not carry
`__all__`** (its absence is their correct static state; nothing star-imports
them, and re-exports ride redundant-alias imports). Do not re-add `__all__` to an
`ava` namespace module — it would be an inert, drifting duplicate and would
reopen the `reportUnsupportedDunderAll` suppressions the split removed. The one
source of truth is `ava.agent_visible_names()`; rationale in
the `__all_for_ava__` split (see git log for design rationale).

`help()` rendered output = `# fqn` Markdown heading + Python stub body. Heading
depth is computed automatically from the number of dots in the FQN: `ava` → `#`, `ava.shell` → `##`, `ava.shell.run`
→ `###`. Non-`ava.*` targets (test fake modules etc.) fall back to H1. FQN source:
- Container (module / SimpleNamespace) → `_qualname` (injected into a plugin namespace
  by `register_namespace`) takes priority, otherwise `__name__`
- Element (function) → `__module__ + . + __name__`; functions wrapped by a plugin
  (e.g. `ava.files.read` replaced by ava_code) go through `_search_ava_for_function_
  binding` scanning `ava.*` to find the binding, and the heading is restored to `ava.files.read`

Body rules:
- Container target → `"""docstring"""` + each child rendered in stub form (function
  `def name(sig): "..."`, submodule `from . import name` + orphan
  docstring, PEP 224 constant `name: type` + orphan docstring, class
  `class name:` + docstring + its own surface expanded — declared fields as
  `name: type`, methods as `def name(sig): "..."`)
- Element target → `def name(sig): """full docstring"""`

A submodule child is the only thing that collapses to just its docstring (it
is its own help target, listed where its parent enumerates it). A class is a
usable surface, so its attributes and methods expand inline, the same way a
function's signature does. A skill child renders as a Markdown heading at its
FQN depth with the one-line description below (skills are documents, not
Python surfaces, so they keep heading form rather than stub form).

## Other rules

1. **Module docstring writes high-level only, never restates children** — `help()` renders
   each child (function / class / submodule) as a stub block, with name + signature
   + its own docstring all there. The module docstring **does not** enumerate child names,
   **does not** restate child behavior, **does not** describe call order ("Use `names()` to list...";
   "`restart` exits the process...") — those belong in the children's docstrings.
   The module docstring only adds what the stub list can't convey: what the module as a whole solves,
   the mental model shared across children, the relationship with other modules (e.g. memory vs files
   division). **One line if one line suffices** — any extra sentence is noise.
   The zero-false-positive core (a child's name inside a backtick span) is
   enforced by `lint_agent_docstrings`; prose use of a child's name as a plain
   English word is fine.
2. **Args / Returns / Raises sections are all optional** —
   - **Don't write types in the docstring** — the renderer injects from type hints into the signature;
     repeating is redundant and drifts from the hint. `prompt (str | None)`, `Returns: int
     — ...` all deleted, leave only semantic descriptions.
   - **Args descriptions only carry non-trivial info** — `path: file path` style filler is not written;
     `prompt: omit to idle and wait for inbound` style behavior differences are written. If the entire
     Args section has nothing to say, **skip the section entirely** (the agent can infer from arg name + type).
   - **Returns descriptions are similarly skippable** — "return id" style self-evident lines are skipped,
     write only when the return value is multi-case string / dict / composite.
   - **Don't write the `optional —` prefix** — a `= None` default carries optional semantics by itself.
3. **Summary: imperative + natural language** — "Start a new agent." not "Starts...";
   "Return its agent id." not "Returns its agent_id." (the underscore is a code
   identifier, not English prose). The summary does not contain a "Returns X" sentence; to describe the return value, open a
   `Returns:` section.
4. **Two docstring paths for module constants** —
   - **PEP 224 attribute docstring** (default) — `AGENT_ID: int\n"""Your
     agent id."""`, snug against the declaration. `help()` AST-parses the source, extracts it, and renders it as
     `## AGENT_ID: int` H2 child. **Suitable for**: values that may be assigned at runtime
     (e.g. `AGENT_ID` resolved from the active turn context); constants no one will pass directly to
     `help(value)`.
   - **`ava.const(value, doc=...)` wrap** (opt-in) — `PATH = ava.const(
     Path.home() / ".ava" / "memory", doc="...")`, returns a `type(value)` subclass
     instance with `__doc__`. **Suitable for**: values that don't change + the agent may directly `help(ava.X.Y)`
     to drill into the docs. Cost: `type(x) is base` fails (use `isinstance` not
     `type ==`); arithmetic / comparison / stream propagation still follow base behavior; `bool`/`NoneType`
     can't be subclassed and will raise.
5. **Don't write Examples** — description + signature is self-evidence. If you need an Example, it usually means
   the description is poorly written — fix the description first.
6. **Error types specific and layered — but rare errors never render** —
   - Don't use generic `ValueError` / `RuntimeError`
   - Parent + subclass design (like ENOENT vs EBUSY): coarse catch uses the parent
     (`except ResurrectError`), fine catch uses the subclass (`except AgentNotFound`)
   - Subclasses correspond to distinguishable "why fail"
   - Re-exported via the SDK namespace (`ava.agents.AgentNotFound`), and kept
     importable via redundant-alias imports — but **out of `__all_for_ava__`**
     (the agent-visible surface, split out from Python's `__all__`): a rare
     failure must not occupy every agent's prompt; its traceback names it
     clearly on the rare occasion it fires.
   - **`Raises:` sections are deleted by default.** Write one only when the
     function must teach an input format anyway (e.g. a cron expression) and
     the error is common and actionable. This inverts the pre-2026-06-10 rule
     ("Raises must be written").
7. **No SDK<->skill coupling** — an SDK docstring never references a skill;
   skill discovery belongs to the skills index section. `ava/skills.py` is the
   one exempt module (enforced by `lint_agent_docstrings`).

`ava/_extend.py` and the other `ava/_*.py` private modules are exceptions (audience is plugin authors, not the agent) — they may keep the dev perspective.
