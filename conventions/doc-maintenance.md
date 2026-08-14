# Doc maintenance

How documentation is structured and maintained. Read when writing or maintaining
project docs.

## Five axes, one fact per place

Every documented fact belongs to exactly one axis, and the axis is identifiable
from the path alone:

| Where | Question it answers | Tense |
|---|---|---|
| `*.ava.okf.md` (next to the code) | what the system **is** — structure, responsibilities, terminology | now |
| `decisions/` | **why** it was chosen this way — rejected alternatives, trade-offs | past, never rewritten |
| `future/` | what we **plan** to do | future |
| `conventions/` | **how** to work — rules, processes, operations | now |
| `traces/` | what the system **does**, in time — one scenario end to end | now, dated to its evidence run — a CLI verb/flag rename is a behavior change: add a record-time CLI-shape disclaimer header and re-verify runnable command blocks |

A fact carried on two axes will drift. When you find a duplicate, keep the copy
on the axis that owns the question and replace the other with a pointer.

## OKF is the source of truth for structure

Anything derivable from the code — modules, endpoints, schemas, wiring, data
flow — lives in the OKF graph, never in `conventions/`. The graph is
**co-located**: most `.ava.okf.md` files sit inside the source trees they
describe (`agent/`, `ava/`, `ava_builtins/`, `cli/`, `frontend/`, `gateway/`,
`services/`, `shared/`); the rest are index-layer nodes in `okf/`.

Hierarchy is filesystem-derived (`shared/okf_graph.py:compute_parent`):
`<dir>/<dir>.ava.okf.md` is the overview node for `<dir>/`, and the other files
inside `<dir>/` are its children (user ruling 2026-08-12: a directory's
overview lives *inside* the directory, not beside it at the parent level).
The sibling position `<dir>.ava.okf.md` at the parent level was retired
2026-08-13 (every nested overview moved inside; `compute_parent` no longer
resolves it) — lint rule E009 fires on any surviving sibling.

Splitting an over-cap node follows the same rule: the child goes in the
directory named after the parent's stem, so the parent edge is derived rather
than asserted. That directory holds only documents when the code it describes
lives elsewhere (`ava_builtins/plugins/ava_fleet/neighbors/`,
`frontend/src/frontend-components/`) — placing the child beside the code instead
would leave it with no filesystem parent, and it would fall back to the root.

The one exception is the **index layer**: the apex and the cross-domain concept
systems — plugins, skills, MCP integration, and the design-phase R1–R4 models —
have no code directory to sit inside, so they live in `okf/`. `compute_parent`
resolves a missing filesystem parent to the root — so the tree has exactly one
root and no dangling edges.

A `[[wikilink]]` is the **edge syntax of the node graph**, so its universe is the
`.ava.okf.md` files and nothing else. A link to any other axis — a decision
record, a plan, a convention — cannot resolve however plainly the file exists,
because those are not nodes and `compute_parent` has nowhere to put them. Cite
them as a normal markdown link or a backticked path
(`[why](../decisions/2026-07-29-okf-node-ceiling.md)`) and keep `[[…]]` for
node-to-node edges. The linter recognises this mistake by name: a target that
matches a real non-node doc reports `W008` saying so, not a bare "not found".

Write a wikilink as either the bare filename or the full repo-relative path. The
resolver's last resort is a **unique-basename** match, which silently rescues a
target whose path is wrong — and stops rescuing it the day a second node takes
that basename, so an untouched link starts failing on someone else's commit.
`W011` (non-blocking) reports a target whose directory component played no part
in its resolution, while it is still only a wrong path.

Format is enforced by `scripts/lint_ava_okf.py`: YAML frontmatter with
`type` / `title` / `description`, a line + character size ceiling (which forces
hierarchy instead of long files), and `[[wikilink]]` targets that must resolve.
The three thresholds are `MAX_LINES` / `MAX_CHARS` / `WARN_MARGIN` in that
script, which is their only source of truth — read them there rather than
trusting a number quoted in prose. In practice the character cap is the one that
binds: no node has ever approached the line cap.

A node with less than `WARN_MARGIN` characters of room left reports `W010`, a
**non-blocking** warning naming its size and remaining room. That is the signal
to plan your next section as a separate node. It is not an instruction to trim
this one — the cap was raised in 2026-07 precisely because trimming to fit had
been deleting documented facts to make room for new ones
([why](../decisions/2026-07-29-okf-node-ceiling.md)).

The ceiling counts **characters of decoded UTF-8** (`len(text)`), not bytes. So
`wc -c` reads high on any node containing multi-byte glyphs — `→`, `✓`, CJK — and
can put a passing file over the cap by tens of characters. Run the linter; do not
eyeball `wc -c` and trim. (Both directions of that mistake have already been made
on `cli.ava.okf.md`: a false alarm at `wc -c` 6036 against a real 5996,
and a commit that trimmed it to fix a violation that did not exist.)

## What does NOT go in the doc axes

Personal, strategic, and deployment-specific material lives in the **private
companion repo** (outside this public tree):

- Personal skills (wechat, gmail, social-media adapters tied to user accounts)
- Deployment instance details (machine roster, IPs, CI fleet config)
- Strategy/competitor notes
- Bench result history and raw prediction data

Ava's doc axes must remain publishable as-is — no personal accounts, no internal
strategy, no deployment secrets. Before writing: "would I be fine with a
stranger reading this on GitHub?"

## Scan mapping: code change → doc to reconcile

Reconcile in the same PR as the code.

Structure changes → the OKF node co-located with the code you touched, plus its
domain overview (the `<dir>/<dir>.ava.okf.md` file inside the domain's
directory) when the domain's shape changed:

| Change | Domain node |
|---|---|
| Agent lifecycle / hibernation / crash-resurrect | `agent/agent.ava.okf.md` |
| SDK surface (`ava/__init__.py`, new namespaces) | `ava/ava.ava.okf.md` |
| Gateway routes / SSE / auth | `gateway/gateway.ava.okf.md` |
| CLI commands / cluster lifecycle | `cli/cli.ava.okf.md` |
| Frontend | `frontend/frontend.ava.okf.md` |
| Shared library / LM providers / config / migrations | `shared/shared.ava.okf.md` |
| Background services | `services/services.ava.okf.md` |
| GitHub Actions / CI workflows | `.github/.github.ava.okf.md` |
| Plugins / extension points | `okf/plugins.ava.okf.md` |
| Skills | `okf/skills/skills.ava.okf.md` |
| MCP integrations | `okf/mcps/mcps.ava.okf.md` |
| Test suite | `tests/tests.ava.okf.md` |
| Ops scripts | `scripts/scripts.ava.okf.md` |
Process, rule, and observed-behaviour changes → the doc that owns them:

| Change | Doc |
|---|---|
| Operational procedures | `.agents/skills/` (one skill per procedure) |
| Backup schedule / retention / restore | `.agents/skills/recover-a-cluster/references/db-restore.md` — the `pg-backup` service's own behaviour moved with the restore procedure rather than staying in the runtime model |
| Runtime model (clusters, data plane, logging, CI) | `runbook.md` |
| Dev environment setup | `dev-setup.md` |
| PR process | `.agents/skills/write-a-pr-description/SKILL.md` |
| Coding conventions | `python-conventions.md` |
| Agent communication style | `communicating-with-user.md` |
| Design philosophy / deliberate omissions | `philosophy.md` + `non-goals.md` |
| SDK docstrings (`ava/*.py` public API) | `sdk-docstring-discipline.md` |
| Lint vs sweeper boundary | `lint-vs-sweeper.md` |
| Behaviour on a path a trace documents | that trace in `traces/` — re-verify against a fresh run and re-date its Evidence, per `.agents/skills/write-a-trace/` |

A directional decision — one that rejected alternatives — also gets a new
`decisions/YYYY-MM-DD-<topic>.md`. Superseding one means writing a new file
and forward-linking from the old, never editing the old.

## Doc language

Follow the project's primary language (inferred from README / existing docs).
