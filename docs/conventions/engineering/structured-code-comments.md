# Structured code comments

Free prose in source is removed or moved to the component's current documentation.
Retain explanations only when they add a constraint, external dependency, decision
or caller contract that names and executable behavior cannot express clearly.
The owning document stays beside the code in `docs/*.meiva.okf.md`.

## Source forms

Python and JavaScript/TypeScript source may contain an exact pointer, without
trailing prose:

```python
# meiva:ref base/agents/context/gateway_client/docs/memory-search.meiva.okf.md#default-timeout
```

JavaScript/TypeScript uses the same payload after `//`. Place it immediately
before the owning definition or within that definition. The tool checks the
resolved owner, not just the document's existence. Ordinary comments, section
banners, commented-out code, unbounded suppression comments, internal docstrings
and unused standalone strings fail the strict source check. There is no file
allowlist or stored legacy baseline. Machine syntax such as a Python entry
shebang or a JavaScript directive remains syntax, not an explanatory comment.
An exact `/** @deprecated */` attached to a TypeScript-recognized declaration
is also machine syntax: the compiler and existing deprecation lint consume it.
Additional prose, other JSDoc tags and unattached markers still fail.

Python line directives have a closed syntax: `# noqa: CODE[, CODE...]` and
`# pyright: ignore[reportRule[, reportRule...]]`. Bare ignores, file-wide settings,
`type: ignore` and trailing prose are not accepted. A strict directive needs an
exact `meiva:ref` bound to its enclosing function, method, class or module. Local
assignments do not own the directive rationale, and an outer or sibling
definition cannot supply it. The document states the actual constraint or trust
boundary; naming a lint rule is not an explanation. Ruff, Pyright and the existing
unused-directive checks retain responsibility for rule validity and necessity.

`# ambient: BATCH` is machine data only on the actual
`gateway_access_from_settings` call line in the ambient audit's non-test scope.
That audit owns the accepted batches and caller inventory; this syntax does not
authorize a new ambient dependency. A stop-window exemption uses exactly
`# quiesce-exempt: meiva:ref path/to/owner.meiva.okf.md#id`, with its reason in that
definition's typed owner. The existing quiesce check still rejects wrong placement
and stale markers. Neither form accepts a free-text explanation in source.

Agent-visible Python SDK docstrings are generated projections of `api` records.
They remain available to `meiva.help`; edit the document owner and run `render-api`.
The static SDK export resolver must prove the actual exported definition. An
internal function cannot preserve its prose by declaring itself an API.

Native pytest fixture docstrings are user-facing help consumed by `--fixtures`
and `--fixtures-per-test`. Keep useful and accurate help on a statically proved
`pytest.fixture` function; no `api` record or runtime `__doc__` assignment is
needed. This narrow consumer does not authorize internal helper docstrings or
body comments. The checker documents its supported decorator and binding proof;
unproved decorators remain strict. Independent review checks the text's value
and truth, including whether changed behavior requires updating existing help.

## Document contracts

A live `meiva-contract` fenced block contains TOML with a unique kebab-case `id`,
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
| `api` | The text callers receive through SDK help | `api` (statically proved `meiva.*` export), exactly one code definition |

Every record needs code bindings within its document's component. Every kind
except `decision` also needs test bindings. Historical records and plans cannot
own current contracts. A contract without a matching source reference or SDK
projection fails. Duplicate API owners, missing or ambiguous symbols, removed
tests, malformed references and changed generated text fail even when the
owning document is unchanged.

The tool checks that a test definition exists, not that it runs or supports the
statement. It does not fetch external links or infer semantic changes from code
hashes. Run affected tests and review their assertions against the statement.
See [the checker](../../../scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md)
for parser boundaries and commands, and the gateway client's
[memory-search contracts](../../../base/agents/context/gateway_client/docs/memory-search.meiva.okf.md)
for complete records.

## Migration and enforcement

The changed gate validates all typed contracts and references, and applies strict
source rules only to new or rewritten notes. An unrelated code edit does not
require migrating existing prose in the file or definition. Compare exact raw
note text within the same path and lexical function/class, matching each old
occurrence once. Copying a note, moving it across owners or paths, and rewriting
its literal makes it new. Unchanged directives do not acquire new rationale
requirements solely because nearby code changes; their actual tool checks remain.

Use the actual PR base, or HEAD versus the staged index for a commit. Both source
sides come from Git blobs. The tracked checkout must match the target snapshot
used by global document/API checks; a mismatch fails explicitly. There is no
stored legacy baseline, per-file allowance or whole-definition cleanup trigger.
Generated sources follow the same rule; fix a generator when its new output
violates it. A deleted target still invalidates remaining references.

Same-owner exact-text reordering is retained, not proof of semantic correctness.
Repeated declarations use their kind, ordinal and sibling-group size to keep
separate matching credit; adding/removing a repeated declaration invalidates only
that group's old identity. Local assignments are not lexical boundaries. The
checker does not reconstruct historical identities from delete-and-rewrite edits.
See the [incremental checker](../../../scripts/content_lint/code_comments/docs/incremental.meiva.okf.md)
for the parser and snapshot boundary.

Python and JavaScript/TypeScript are the implemented language boundary. Shell,
YAML, Rust and other languages do not yet have strict comment parsing. Do not
claim that this change bans all comments in all languages. Merged migrations
remain immutable; this policy does not authorize rewriting their history.

The full `inventory`/`check` commands still report existing source prose and fail
while findings remain. They are explicit audits, not the changed gate's scope.
A passing incremental check does not certify complete migration or semantic truth.

## Independent semantic review

For every contribution, an independent reviewer reads the actual diff, the
owning documents and the relevant test assertions. They assess whether prose
should be deleted, moved or rewritten; whether each retained claim is true; and
whether changed behavior requires an unchanged contract to be reconciled.
Deleting useful rationale without a replacement is a finding too. Follow the
review requirement in [contributing](../../contributing.md).

This is a contributor workflow requirement. CI performs deterministic checks;
it does not call a model, issue a semantic approval or require an attestation.
