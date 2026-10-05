# Doc maintenance

How documentation is structured and maintained. Read when writing or maintaining
project docs.

## Five axes, one fact per place

Every documented fact belongs to exactly one axis, and the axis is identifiable
from the path alone:

| Where | Question it answers | Tense |
|---|---|---|
| `*.ava.okf.md` (in the package's `docs/`) | what the system **is** — structure, responsibilities, terminology | now |
| `decisions/` | **why** it was chosen this way — rejected alternatives, trade-offs | past, never rewritten |
| `future/` | what we **plan** to do | future |
| `conventions/` | **how** to work — rules, processes, operations | now |
| `postmortems/` | **why a failure escaped** — what broke, why every safety net missed it, what guardrail now prevents the class | past, never rewritten |

A fact carried on two axes will drift. When you find a duplicate, keep the copy
on the axis that owns the question and replace the other with a pointer.

### What the system *does* is not an axis

There was a fifth axis, `traces/` — one real recorded run, annotated. It is
retired ([why](../decisions/2026-08-19-retire-the-traces-doc-axis.md)) and
nothing trace-shaped is committed to this repo. A committed run is a fact about
a version that has already moved: it rots on every CLI rename and behavior
change, and the evidence it copied is still queryable, and current, in the
checkpoints table and the observability stack.

Do not re-create it. A question about what a run did is answered by querying
that run — `.agents/skills/inspect-a-trace/SKILL.md` is the know-how for doing
so across checkpoints, Loki, and Tempo. What generalizes out of a run belongs
on an axis: a structural fact in the package's OKF node, a rule in
`conventions/`, a rejected alternative in `decisions/`.

## Postmortems and defensive patterns

`decisions/` and `postmortems/` are both past-tense and both frozen, and they are
easy to confuse. The split is the question, not the tense: a decision is a
point-in-time **choice** with alternatives rejected; a postmortem is an **escape
analysis** — the safety nets that should have caught something and did not. A
change that rejected an option is a decision even if it followed an incident; an
incident whose value is "here is why nothing caught it" is a postmortem even if
no alternative was ever on the table.

**Entry bar — all three at once:** subtle (the mechanism had to be re-derived the
hard way), systemic (it escaped through a gap in tests, tooling, or conventions,
not one person's slip), and costly to rediscover (hours, or a production
incident). A routine bug that a test caught stays in git history.

**Shape.** `postmortems/NNNN-<kebab-title>.md`, numbered, executive-summary
first. Copy `postmortems/_template.md`, which carries the sections: summary,
timeline, root cause (mechanism *plus* the per-safety-net escape analysis),
guardrails added, lessons. Postmortems are the one tier where war-story
chronology belongs — current-state facts stay on the other axes, cited by link.

**The pipeline.** A postmortem produces guardrails; the guardrails that
generalize condense into one 3–6 line rule in
[`defensive-patterns.md`](defensive-patterns.md), which is the page people
re-read. Neither half works alone: the rules without the stories lose their
evidence and decay into vibes, and the stories without the page never compress
into anything anyone reads twice. A postmortem whose lesson generalizes and does
not appear on that page is only half filed.

Like `decisions/`, `postmortems/` is skipped entirely by
`scripts/content_lint/check_doc_references.py` — naming the flag or file that existed at
incident time is the record working as intended. Nothing checks those links, so
mark what a reader cannot open; commits and PR numbers predating the 2026-08-18
public-repo cutover are not reachable from public `main` and are labelled
`(pre-cutover)`.

## OKF is the source of truth for structure

Anything derivable from the code — modules, endpoints, schemas, wiring, data
flow — lives in the OKF graph, never in `conventions/`. Each package keeps its
`.ava.okf.md` files in its own `docs/` directory, beside its code and `tests/`
(`agent/`, `ava/`, `ava_builtins/`, `cli/`, `ui/web/`, `gateway/`, `services/`,
`base/`); the rest are index-layer nodes in `okf/`.

Hierarchy is filesystem-derived (`base/packages/docs/okf_graph.py:compute_parent`)
and runs on **logical paths**: the `docs/` layer is transparent, so the last
`docs` directory segment is dropped from a node's path (`logical_path`) —
`agent/graph/docs/graph.ava.okf.md` sits at `agent/graph/graph.ava.okf.md`. On
logical paths, `<dir>/<dir>.ava.okf.md` is the overview node for `<dir>/`, and
the other files inside `<dir>/` are its children (user ruling 2026-08-12: a directory's
overview lives *inside* the directory, not beside it at the parent level).
`compute_parent` does not resolve a sibling `<dir>.ava.okf.md` at the parent
level; lint rule E009 fires on one, layered or not. Links are not logical:
`[[…]]` and relative markdown links name the real path, `docs/` included.

Splitting an over-cap node follows the same rule: the child goes in the
directory named after the parent's stem — inside the `docs/` layer, so
`docs/<stem>/` — and the parent edge is derived rather than asserted. That
directory holds only documents when the code it describes lives elsewhere
(`ava_builtins/plugins/ava_fleet/docs/neighbors/`,
`ui/web/src/docs/frontend-components/`); a child placed beside its parent in the
layer would attach to the directory's overview (or the root when there is none),
not to the node it was split from.

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

Format is enforced by `scripts/content_lint/lint_ava_okf.py`: YAML frontmatter with
`type` / `title` / `description`, a line + character size ceiling (which forces
hierarchy instead of long files), and `[[wikilink]]` targets that must resolve.
Every node sits in a `docs/` layer (`E014`; `okf/` and `.github/` are exempt), and
the overview-position rule (`E009`) is judged on logical paths, so a directory
whose nodes all sit in `docs/<sub>/` still has its overview there and no sibling
`docs/<sub>.ava.okf.md`. The three thresholds are `MAX_LINES` / `MAX_CHARS` / `WARN_MARGIN` in that
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

Personal, strategic, and deployment-specific material does not belong in
this public tree — keep it in your own private storage (a private skills
repo, the cluster memory pool):

- Personal skills (IM, mail, social-media adapters tied to user accounts)
- Deployment instance details (machine roster, IPs, CI fleet config)
- Strategy/competitor notes
- Raw run/result histories of your own deployment

Ava's doc axes must remain publishable as-is — no personal accounts, no internal
strategy, no deployment secrets. Before writing: "would I be fine with a
stranger reading this on GitHub?"

## Scan mapping: code change → doc to reconcile

Reconcile in the same PR as the code.

Structure changes → the OKF node in the `docs/` of the package you touched, plus
its domain overview (the `<dir>/<dir>.ava.okf.md` node of the domain's
directory) when the domain's shape changed. Paths below are the files' real
paths, `docs/` layer included:

| Change | Domain node |
|---|---|
| Agent lifecycle / crash-resurrect | `agent/docs/agent.ava.okf.md` |
| SDK surface (`ava/__init__.py`, new namespaces) | `ava/docs/ava.ava.okf.md` |
| Gateway routes / SSE / auth | `gateway/docs/gateway.ava.okf.md` |
| CLI commands / cluster lifecycle | `cli/docs/cli.ava.okf.md` |
| Frontend | `ui/web/docs/web.ava.okf.md` |
| Base library / LM providers / config / migrations | `base/docs/base.ava.okf.md` |
| Background services | `services/docs/services.ava.okf.md` |
| GitHub Actions / CI workflows | `.github/.github.ava.okf.md` |
| Plugins / extension points | `okf/plugins/plugins.ava.okf.md` |
| Skills | `okf/skills/skills.ava.okf.md` |
| MCP integrations | `okf/mcps/mcps.ava.okf.md` |
| Test suite | `tests/docs/tests.ava.okf.md` |
| Ops scripts | `scripts/docs/scripts.ava.okf.md` |
Process, rule, and observed-behaviour changes → the doc that owns them:

| Change | Doc |
|---|---|
| Operational procedures | `.agents/skills/` (one skill per procedure) |
| Backup schedule / retention / restore | `.agents/skills/operating-ava-cluster/references/db-restore.md` — the `pg-backup` service's own behaviour moved with the restore procedure rather than staying in the runtime model |
| Runtime model (clusters, data plane, logging, CI) | `runbook.md` |
| Dev environment setup | `dev-setup.md` |
| PR process | `CONTRIBUTING.md` |
| Coding conventions | `python-conventions.md` |
| Agent communication style | `communicating-with-user.md` |
| Design philosophy / deliberate omissions | `philosophy.md` + `non-goals.md` |
| SDK docstrings (`ava/*.py` public API) | `sdk-docstring-discipline.md` |
| Lint vs sweeper boundary | `lint-vs-sweeper.md` |
| Observability surfaces (event fields, span attributes, query recipes) | `.agents/skills/inspect-a-trace/` — the skill IS the reconcile target, since nothing else records what a run looks like |
| A bug class that already shipped here | `defensive-patterns.md` — plus a `postmortems/` entry when it clears the entry bar |

A directional decision — one that rejected alternatives — also gets a new
`decisions/YYYY-MM-DD-<topic>.md`. Superseding one means writing a new file
and forward-linking from the old, never editing the old.

A failure that cleared the entry bar above also gets a new
`postmortems/NNNN-<kebab-title>.md`, and its generalizable lesson an entry in
`defensive-patterns.md`. Same freeze rule: a postmortem is never rewritten to
match today's code.

## Doc language

Follow the project's primary language (inferred from README / existing docs).
