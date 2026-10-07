# Technical-debt inspection

Use these rules when a change exposes debt or someone requests a debt audit.
They are current project guidance, not a required sweep before contributing or
merging. Choose the classes relevant to the request. The single register is
[the debt ledger](../../../future/tech-debt/ledger.md); keep existing fingerprints
and evidence, respect `wontfix`, and delete resolved entries only after checking
their evidence. A file move alone does not resolve an issue or advance the
ledger's verification watermark.

A confirmed finding names concrete files or symbols and the defect or shared
decision. A grep hit, old date, available upgrade, or co-change score is a
candidate, not proof. If a broad repair is outside the authorized change,
record actionable evidence in the ledger. Do not create a parallel tracker.
The optional [generic sweeper skill](../../../ava_builtins/skills/practice/sweeper/SKILL.md)
can reconcile the ledger using this document as its project input.

## Rule ownership

The [lint boundary](lint-vs-sweeper.md) separates deterministic violations from
judgment. Existing automated owners remain authoritative; repeat scans do not
add protection.

| Class | Automated owner | Remaining judgment |
|---|---|---|
| deps | Locked installs and dependency checks verify selected versions | Assess upgrade compatibility, support and risk |
| docs-aging | Reference, symbol, anchor and roster lints guard current facts | Compare plan status with shipped behavior |
| fail-fast | Ruff S110 and `lint-no-silent-failures` reject silent exceptions | Distinguish invalid model defaults from valid input boundaries |
| inline-marker | No universal prohibition on TODO/FIXME/XXX/HACK | Verify the unfinished behavior and its consequence |
| dead-code | No zero-false-positive framework-wide dead-code gate | Prove a candidate has no runtime consumers |
| boundary | Import layering and structure lints guard declared boundaries | Identify overlapping responsibilities or missing owners |
| skill-desc | `lint_skill_descriptions.py` enforces 80 units | Tighten descriptions in the 50–80 unit soft zone |
| docstring-budget | `agent_docstrings.py` enforces its deterministic rules | Justify rare errors, narration and soft length budgets |
| stalecopy | Reference lints detect broken targets, not copied stale facts | Compare copied prose with changes to its original |
| rebase-bypassed hooks | `check-merge-conflict`, repeated by structural CI | No separate debt scan for already-enforced markers |
| locality | Structure lint guards package doors and declared decision owners | Confirm shared decisions from historical co-change candidates |

## Inspection guidance

### Dependencies

Run `uv pip list --outdated` in the prepared checkout and `npm outdated --json`
in `ui/web`. Use the locked environment and real `node_modules`; missing
`current` versions are not upgrade evidence. Follow [development setup](../dev-setup.md)
rather than sharing a virtualenv or replacing the lockfile. Assess minor and
major upgrades; stable runtime upgrades still need the approval required by
AGENTS.md. Do not add every outdated package to the ledger: record actionable
risk or blocked upgrades. Keep full mechanical lists in the audit report.

### Plan aging and inline markers

For relevant files under `future/`, compare declared status, `Superseded by`
and shipped markers against implementation, the last file change and referenced
PRs. Age alone does not establish staleness. Inspect source TODO/FIXME/XXX/HACK
markers with their surrounding behavior and blame history; ordinary comments
and deliberate limitations are not violations.

### Fail-fast candidates

Search tracked source for `.get(...) or {}`, wildcard `case _:` branches, and
comments such as "shouldn't happen". Validate the boundary: config defaults,
external inputs and non-enum matching can legitimately use these forms. Unknown
internal states must fail; do not ban every default mechanically. Silent
exception checks already belong to Ruff and the diagnostics lint.

### Dead code

Vulture can suggest candidates at `--min-confidence 80`; it cannot prove
absence of dynamic registration. Exclude framework registrations only after
reading their consumers: FastAPI handlers, SDK exports, pytest fixtures,
LangGraph nodes, plugin callbacks, wire schemas, watchdog methods and transport
protocol signatures. Check parse failures before claiming scan coverage. Do
not delete code from confidence scores or expand automation to new frameworks
without evidence.

### Boundaries and locality

Read recent relevant commits and their consumers for unclear ownership and
cross-package changes. `scripts/structure/cochange.py` is an optional report,
not a gate. It uses the structure lint's package resolution and excludes known
cross-process schema contracts. Its default first-parent window is 90 days,
minimum support 8 and minimum confidence 0.6; use `--commits`, `--days` or
`--json` for a scoped investigation.

Spread metrics and file pairs identify where to read. Confirm a locality
finding in at least two commits, recording support/confidence, the commit SHAs
and the particular decision both sides must agree on. Use fingerprint
`locality:<file-a>:<file-b>`. After establishing one owner, register that decision
in `scripts/structure/locality.py` so the existing gate protects it; only then
resolve the ledger item.

### Skill descriptions and SDK docstrings

Use the existing scanners' scope and measurements, not a second file list.
`lint_skill_descriptions.py` owns `length_units` and `_skill_entries`; description
trimming must preserve capability and trigger information. For SDK docstrings,
use the surface discovery and visibility helpers in `agent_docstrings.py`.
Read [SDK docstring discipline](../sdk-docstring-discipline.md). Function docstrings
over 12 lines, module docstrings over 2 lines and `Raises:` sections invite
review, not automatic deletion. Input formats and common actionable errors can
justify longer text. Record a deliberate exception as `wontfix` with its reason
so a subsequent requested inspection does not re-litigate it.

### Copied documentation

When rebasing a documentation move, compare the source's changes since the
merge base with the new copy. A conflict-free rebase can retain superseded
prose. Confirm the actual fact and its owning document before restoring or
removing text; a removed-line match alone does not establish staleness.

## Optional candidate report

`bash scripts/audit/tech_debt_candidates.sh [--repo PATH]` preserves the existing
manual/network candidate report used by the configured daily-debt schedule.
It reports dependency, fail-fast, inline-marker, dead-code, docstring and locality
candidates. Some commands need network access and external tools. Read scan
errors and exclusions; absence of printed hits is not proof of correctness.
The report opens no PR, records no findings, and duplicates no silent-exception
or conflict-marker gate. It is not a contribution prerequisite. The schedule
is an existing runtime option; this guide does not enable or run it.

Each section reports `findings`, `empty`, `error` or `skipped`, preserving raw
stdout/stderr evidence. Findings are not command failures. Any tool or report
parse error marks the overall scan incomplete and returns nonzero, which the
schedule records as a failed scan; missing frontend dependencies are an explicit
optional skip. The scanner remains an optional discovery aid, not a lint gate.

Exit-code interpretation follows the tools: ripgrep 1 means no matches and
greater values are errors; [npm outdated's implementation](https://github.com/npm/cli/blob/latest/lib/commands/outdated.js)
returns 1 for outdated dependencies, verified against its JSON report;
[Vulture's documented codes](https://github.com/jendrikseipp/vulture#exit-codes)
use 3 for findings and 1/2 for input/argument errors. Invalid JSON or an error
envelope is never reported as an empty dependency scan.
