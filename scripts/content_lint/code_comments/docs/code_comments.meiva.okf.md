---
type: doc
title: Structured code comment checks
description: Parsed source references, colocated typed contracts and generated SDK documentation.
tags: [scripts, lint, documentation]
---

# Structured code comment checks

`model.py` and `records.py` define the closed document contract. `check.py` binds
records to tracked, canonical, regular files, exact qualified symbols and test
definitions. The tracked universe comes from meizel; Python AST/tokenize and the
locked frontend TypeScript compiler parse source without importing application
code. `typescript_index.py` validates the compiler output and source hashes.

`render.py` uses the existing static SDK provenance resolver and generates API
docstrings from documentation. It verifies that executable ASTs are unchanged
and prepares every file before writing.

## Commands

Run from the repository with its real `.venv` and installed frontend dependencies:

```bash
bash scripts/content_lint/code_comments/check.sh validate
bash scripts/content_lint/code_comments/check.sh check path/to/source.py
bash scripts/content_lint/code_comments/check.sh inventory
bash scripts/content_lint/code_comments/check.sh render-api
bash scripts/content_lint/code_comments/check.sh changed
bash scripts/content_lint/code_comments/check.sh changed BASE_REF
```

`validate` checks all typed records and references. `check` additionally rejects
free prose in selected sources; without paths it selects the whole supported
tree. `inventory` is the full strict scan with failure status while work remains;
pass `--json` before the command for structured output. The CLI prints its scope.

`changed` checks only new or rewritten notes while validating every typed record.
It compares HEAD/index for staged work or base/HEAD for committed work, requiring
a matching tracked checkout. The shell passes pre-commit's from/to values as
explicit arguments; Git resolution and validation have one owner in `changes.py`.
See [incremental checks](incremental.meiva.okf.md) for exact matching, repeated
lexical declarations, copies/renames, snapshot failures and validation boundaries.

## Parser boundaries

Python bindings include qualified definitions and simple assignments; ambiguous
definitions fail. Entry shebangs recognize exactly `/usr/bin/env python`,
`/usr/bin/env python3`, `/usr/bin/env python3.12`, `/usr/bin/python3` and
`/usr/bin/python3.12` at the start of the first line. Interpreter arguments,
trailing prose and the same text at another position remain comments.
API ownership reuses `scripts/codegen/sdk_surface`, so dynamic
exports which that resolver cannot prove cannot authorize generated docstrings.
PEP 224 constant documentation is projected beside its assignment. A statically
proved module-class property can instead project beside its unique module-level
annotation-only declaration, matching the source read by SDK help. Other
annotation-only dynamic attributes remain blocked when the provenance resolver
cannot prove them; preserve their existing help rather than dropping it.
JavaScript/TypeScript typed bindings include named declarations and literal test labels
from imported Vitest, Playwright and node:test runners. Unsupported dynamic test
labels and wrappers do not resolve; use a concrete test definition. An exact
`/** @deprecated */` is machine syntax only when TypeScript attaches it to a
named declaration symbol. Prose-bearing or unattached JSDoc remains a finding.

Python lint directives retain their exact tool syntax and bind rationale to the
nearest lexical function/class, or the module outside a definition. This is
separate from assignment ownership used by ordinary references and SDK attribute
help. Every declaration span is retained, including repeated qualified names;
an inline directive cannot borrow the next definition's rationale. The ambient
audit retains its own batch vocabulary and caller inventory;
the parser only admits a single batch token on a call that audit examines.
Quiesce markers use an exact typed reference; the existing stop-window lint still
owns loop placement and stale-marker checks. No rule registry, runtime import or
path exemption is added. Unchanged directives are not globally migrated by
reference-only validation.

See [native fixture help](fixtures.meiva.okf.md) for the bounded pytest consumer.

The checker proves syntax and reference integrity, not external-source freshness,
test execution or semantic truth. The contribution's independent review owns
those judgments. The current migration and reviewer rules are in
[structured code comments](../../../../docs/conventions/engineering/structured-code-comments.md).

```meiva-contract
id = "python-entry-shebang"
kind = "invariant"
statement = "A Python shebang is syntax only at the start of the first line and only for the exact supported interpreter forms, including the unversioned /usr/bin/env python entry. Arguments, trailing prose, forged interpreters and misplaced shebangs remain rejected comments."
code = ["scripts/content_lint/code_comments/python_source.py:parse_python"]
tests = ["scripts/content_lint/code_comments/tests/test_check.py::test_python_entry_shebang_is_syntax", "scripts/content_lint/code_comments/tests/test_check.py::test_python_shebang_does_not_hide_comments"]
```

```meiva-contract
id = "api-help-owner"
kind = "invariant"
statement = "An API contract binds the documentation owner proved by the canonical SDK resolver. A module property's annotation can own PEP 224 help without weakening ambiguous getter/setter rejection. Rendering preserves executable syntax and leaves source untouched when provenance is unproved."
code = ["scripts/content_lint/code_comments/check.py:check_api"]
tests = ["scripts/content_lint/code_comments/tests/test_api.py::test_module_property_help_is_projected_at_the_annotation", "scripts/content_lint/code_comments/tests/test_api.py::test_unproved_property_help_remains_untouched", "scripts/content_lint/code_comments/tests/test_api.py::test_unproved_dynamic_attribute_fails_without_deleting_help"]
```

Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].

```meiva-contract
id = "python-machine-directives"
kind = "invariant"
statement = "Strict Python lint directives contain only explicit Ruff or Pyright rule lists and use a valid rationale bound to the same lexical definition, never a local assignment, sibling or enclosing definition. Ambient markers remain audited call data and quiesce markers remain exact bound references consumed by the existing stop-window check. Ordinary prose and unbounded suppressions remain rejected."
code = ["scripts/content_lint/code_comments/directives.py:ambient_call_lines", "scripts/content_lint/code_comments/directives.py:comment_kind", "scripts/content_lint/code_comments/check.py:check_notes"]
tests = ["scripts/content_lint/code_comments/tests/test_directives.py::test_line_directive_uses_its_definition_contract", "scripts/content_lint/code_comments/tests/test_directives.py::test_directive_cannot_borrow_sibling_or_outer_rationale", "scripts/content_lint/code_comments/tests/test_directives.py::test_signature_and_method_directives_bind_the_method", "scripts/content_lint/code_comments/tests/test_directives.py::test_unbounded_or_prose_directives_stay_rejected", "scripts/content_lint/code_comments/tests/test_directives.py::test_ambient_marker_only_names_an_audited_call", "scripts/content_lint/code_comments/tests/test_directives.py::test_quiesce_consumer_keeps_loop_and_stale_marker_checks", "scripts/content_lint/code_comments/tests/test_directives.py::test_duplicate_definition_cannot_borrow_enclosing_rationale", "scripts/content_lint/code_comments/tests/test_directives.py::test_module_directive_cannot_borrow_the_next_function"]
```
