# Structured code comments

Free prose in source is removed or moved to the component's current documentation.
Retain explanations only when they add a constraint, external dependency, decision
or caller contract that names and executable behavior cannot express clearly.
The owning document stays beside the code in `docs/*.ava.okf.md`.

## Source forms

Python and JavaScript/TypeScript source may contain an exact pointer, without
trailing prose:

```python
# ava:ref base/agents/context/gateway_client/docs/memory-search.ava.okf.md#default-timeout
```

JavaScript/TypeScript uses the same payload after `//`. Place it immediately
before the owning definition or within that definition. The tool checks the
resolved owner, not just the document's existence. Ordinary comments, section
banners, commented-out code, suppression comments, internal docstrings and unused
standalone strings fail the strict source check. There is no suppression syntax,
file allowlist or stored legacy baseline. Machine syntax such as a Python entry
shebang or a JavaScript directive remains syntax, not an explanatory comment.

Agent-visible Python SDK docstrings are generated projections of `api` records.
They remain available to `ava.help`; edit the document owner and run `render-api`.
The static SDK export resolver must prove the actual exported definition. An
internal function cannot preserve its prose by declaring itself an API.

## Document contracts

A live `ava-contract` fenced block contains TOML with a unique kebab-case `id`,
`kind`, a nonempty `statement`, exact `code` definitions and exact `tests`.
Unknown fields and unknown kinds fail. Code anchors join a tracked source path and qualified symbol with a colon;
`<module>` names the module. Test anchors use pytest's
`path/test_file.py::TestClass::test_method`, or a JavaScript test file followed
by literal imported-runner suite and test labels separated by `::`.

| Kind | Purpose | Additional fields |
| --- | --- | --- |
| `invariant` | A current behavioral or ordering constraint | None |
| `external` | A constraint imposed by a dependency or protocol | `source` (HTTPS), `applies_to` (version or scope) |
| `decision` | A pointer to a recorded architectural choice | `decision` (exact existing decision document) |
| `api` | The text callers receive through SDK help | `api` (statically proved `ava.*` export), exactly one code definition |

Every record needs code bindings within its document's component. Every kind
except `decision` also needs test bindings. Historical records and plans cannot
own current contracts. A contract without a matching source reference or SDK
projection fails. Duplicate API owners, missing or ambiguous symbols, removed
tests, malformed references and changed generated text fail even when the
owning document is unchanged.

The tool checks that a test definition exists, not that it runs or supports the
statement. It does not fetch external links or infer semantic changes from code
hashes. Run affected tests and review their assertions against the statement.
See [the checker](../../../scripts/content_lint/code_comments/docs/code_comments.ava.okf.md)
for parser boundaries and commands, and the gateway client's
[memory-search contracts](../../../base/agents/context/gateway_client/docs/memory-search.ava.okf.md)
for complete records.

## Migration and enforcement

The initial gate validates all typed contracts and references, and applies the
strict rule to every added, copied, renamed or modified supported source file
in the change, including Git type changes, tests and generated source. Touching one line migrates
the whole file. Fix a generator when its output violates the rule. A deleted
target invalidates remaining references. Existing untouched free comments remain
migration work; a passing changed-file gate does not mean the whole tree is clean.

Python and JavaScript/TypeScript are the implemented language boundary. Shell,
YAML, Rust and other languages do not yet have strict comment parsing. Do not
claim that this change bans all comments in all languages. Merged migrations
remain immutable; this policy does not authorize rewriting their history.

The full `inventory`/`check` commands report outstanding source migration and
fail while it remains. Complete migration in reviewable batches before switching
the default gate to whole-tree strict mode. There is no per-file grandfathering
entry to renew or forget.

## Independent semantic review

For every contribution, an independent reviewer reads the actual diff, the
owning documents and the relevant test assertions. They assess whether prose
should be deleted, moved or rewritten; whether each retained claim is true; and
whether changed behavior requires an unchanged contract to be reconciled.
Deleting useful rationale without a replacement is a finding too. Follow the
review requirement in [contributing](../../contributing.md).

This is a contributor workflow requirement. CI performs deterministic checks;
it does not call a model, issue a semantic approval or require an attestation.
