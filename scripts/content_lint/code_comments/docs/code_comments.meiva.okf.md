---
type: doc
title: Structured code comment checks
description: Parseable source notes with file links, incremental strictness and a review corpus.
tags: [scripts, lint, documentation]
---

# Structured code comment checks

`model.py` defines notes and findings. `check.py` binds every `meiva:link` to a
tracked, canonical, regular file and rejects free prose. `corpus.py` extracts the
notes for review. The tracked universe comes from meizel; Python AST/tokenize and
the locked frontend TypeScript compiler parse source without importing
application code. `typescript_index.py` validates the compiler output and source
hashes.

## The one source form

```python
# meiva:note
# The gateway re-resolves the principal on every refresh, so a cached token
# cannot outlive a role change.
# meiva:link base/agents/context/gateway_client/docs/memory-search.meiva.okf.md
def refresh() -> None: ...
```

`meiva:note` opens a block; the following comment lines at the same column are
its body, and `meiva:link` lines follow it. A link alone, with no body, points a
code site at shared documentation. Python uses `#`, JavaScript and TypeScript use
`//`. A block binds to its innermost enclosing declaration, or to the next
declaration when it stands alone before one.

Links are file-level: they name repository paths, not document anchors or test
nodes. The checker proves that the file is tracked and regular; it does not judge
whether the reader follows the right pointer.

## Commands

Run from the repository with its real `.venv` and installed frontend dependencies:

```bash
bash scripts/content_lint/code_comments/check.sh validate
bash scripts/content_lint/code_comments/check.sh check path/to/source.py
bash scripts/content_lint/code_comments/check.sh inventory
bash scripts/content_lint/code_comments/check.sh changed
bash scripts/content_lint/code_comments/check.sh corpus --format markdown
```

`validate` parses every source that carries a directive and checks its links.
`check` additionally rejects free prose in selected sources; without paths it
selects the whole supported tree. `inventory` is the full strict scan with failure
status while work remains; pass `--json` before the command for structured output.
`corpus` prints every note as JSONL or a Markdown report with length distribution
and repeated text. The CLI prints its scope.

`changed` checks only new or rewritten notes while validating every link. It
compares HEAD/index for staged work or base/HEAD for committed work, requiring a
matching tracked checkout. The shell passes pre-commit's from/to values as
explicit arguments; Git resolution and validation have one owner in `changes.py`.
See [incremental checks](incremental.meiva.okf.md) for exact matching, repeated
lexical declarations, copies/renames, snapshot failures and validation boundaries.

## Parser boundaries

Python bindings include qualified definitions and simple assignments; ambiguous
definitions fail. Entry shebangs recognize exactly `/usr/bin/env python`,
`/usr/bin/env python3`, `/usr/bin/env python3.12`, `/usr/bin/python3` and
`/usr/bin/python3.12` at the start of the first line. Interpreter arguments,
trailing prose and the same text at another position remain comments.
JavaScript/TypeScript typed bindings include named declarations and literal test labels
from imported Vitest, Playwright and node:test runners. Unsupported dynamic test
labels and wrappers do not resolve; use a concrete test definition. An exact
`/** @deprecated */` is machine syntax only when TypeScript attaches it to a
named declaration symbol. Prose-bearing or unattached JSDoc remains a finding.

Python lint directives retain their exact tool syntax and need a `meiva:note`
bound to the same lexical owner, whose text or link states why. This is separate from assignment
ownership used by ordinary notes. Every declaration span is retained, including
repeated qualified names; an inline directive cannot borrow the next definition's
rationale. The ambient audit retains its own batch vocabulary and caller
inventory; the parser only admits a single batch token on a call that audit
examines. `# quiesce-exempt: <reason>` keeps the exact form the stop-window lint
matches; that lint still owns loop placement and stale-marker checks. No rule registry, runtime
import or path exemption is added.

Docstrings remain prose in source and fail the strict check, except pytest
fixture help and the agent SDK surface, whose docstrings are the literal text
`meiva.help` renders. Those docstrings are the single source of SDK help; there is
no generated projection. See [native fixture help](fixtures.meiva.okf.md) and
[SDK docstring discipline](../../../../docs/conventions/sdk-docstring-discipline.md).

The checker proves syntax and link integrity, not semantic truth, test execution
or whether a note is still accurate after behaviour changes. The contribution's
independent review owns those judgments. The current reviewer rules are in
[structured code comments](../../../../docs/conventions/engineering/structured-code-comments.md).

Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].
