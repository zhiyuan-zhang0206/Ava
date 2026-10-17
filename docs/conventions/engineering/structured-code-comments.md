# Structured code comments

Free prose in source is removed or moves to the component's current documentation.
An explanation stays in source only when it passes the exceptions below.

## One rule: put the explanation where its readers are

An explanation that one code site needs lives at that site. An explanation that
several sites share lives in the owning document, and each site links to it.
Nothing else decides placement: not the file type, not the directory, not a
classification field.

## Source form

Python uses `#`, JavaScript and TypeScript use `//`.

```python
# meiva:note
# The gateway re-resolves the principal on every refresh, so a cached token
# cannot outlive a role change.
# meiva:link base/agents/context/gateway_client/docs/memory-search.meiva.okf.md
def refresh() -> None: ...
```

`meiva:note` stands alone on its line and opens a block. The following comment
lines at the same column are its body; a bare comment line separates paragraphs. Zero
or more `meiva:link` lines close the block:

```python
# meiva:link base/sessions/docs/session-backend.meiva.okf.md
def kill_session(...) -> None: ...
```

A link alone points a code site at shared documentation. That is how one
explanation serves several definitions without being copied.

Block placement binds the note: the parser takes the innermost enclosing
declaration, or the declaration that immediately follows a standalone block. A
note inside a function body belongs to that function.

Links name repository paths only. The target must be a tracked regular file; a
missing file fails the check. There are no anchors, no test-node suffixes and no
URLs. A link is navigation, so review judges whether it points somewhere useful.

A block carries one fact. There is no length limit: an arbitrary cap would force
rewriting text that is already reviewed, and length outliers are visible in the
corpus report instead.

## What still fails

Ordinary comments, section banners, commented-out code, internal docstrings and
unused standalone strings fail the strict check. There is no file allowlist and no
stored legacy baseline.

Machine syntax is not prose and stays:

- a Python entry shebang (`/usr/bin/env python`, `... python3`, `... python3.12`,
  `/usr/bin/python3`, `/usr/bin/python3.12`) at the start of the first line
- `# noqa: CODE[, CODE...]` and `# pyright: ignore[reportRule[, ...]]`
- `# quiesce-exempt: <reason>` (the stop-window lint owns the reason and the placement)
- `# ambient: BATCH` on the call line the ambient audit examines
- an exact `/** @deprecated */` attached to a TypeScript declaration

A lint directive needs a `meiva:note` owned by the same definition. Its text, or
the document its link names, states the constraint; naming a lint rule is not an
explanation. A quiesce
marker carries its own reason because the stop-window lint matches that exact
form. Ruff, Pyright and the existing stop-window lint keep ownership of
rule validity, necessity and placement.

Pytest fixture docstrings are user-facing `--fixtures` help and stay on the
fixture function. Agent-visible SDK docstrings are the `meiva.help` text itself;
see [SDK docstring discipline](../sdk-docstring-discipline.md). Both are the only
docstring exceptions: other internal docstrings remain findings.

## Checks

```bash
bash scripts/content_lint/code_comments/check.sh validate
bash scripts/content_lint/code_comments/check.sh check path/to/source.py
bash scripts/content_lint/code_comments/check.sh inventory
bash scripts/content_lint/code_comments/check.sh changed
bash scripts/content_lint/code_comments/check.sh corpus --format markdown
```

`validate` proves every link. `check` also rejects free prose; without paths it
selects the whole supported tree and reports the outstanding legacy inventory.
`changed` is the gate: it strictly checks only notes that are new, rewritten,
copied or moved into another declaration, while still validating every link.
Unchanged legacy prose does not block an unrelated code edit.

`corpus` extracts every note as JSONL, or as a Markdown report with the length
distribution, repeated text and per-note links. A pull request that adds or
rewrites notes includes its corpus extract, so review reads the notes as a list
instead of hunting through the diff.

The tools prove syntax and link integrity. They do not prove that a note is true,
that its test still covers it, or that unchanged prose survived a behaviour
change. Independent review owns those judgments.

## SDK help

SDK help is not a comment form and has no generated projection: the docstring on
an agent-visible name is the only copy of the text `meiva.help` renders.
`scripts/lint/agent_docstrings.py` owns its content rules.
