# Static import ownership

`__init__.py` owns normalized clauses, package anchors, bindings and direct
dependency resolution. `cache.py` stores normalized production statements;
resolved module edges always use the current checkout.

`executed.py` supplies bounded facts about actual Python `-c` inputs. It follows
literal source, one plain binding and local undecorated helpers that pass a
source parameter unchanged or through one plain local binding. A positional
source before `*argv` and a keyword-only source remain independent of trailing
argument data. `**kwargs` and unpacking before a positional source stay unknown.
Literal `-W`/`-X` operands are consumed as interpreter options; source selection
stops at a script, `-m` or `--`. Launchers and Python executables must resolve
through their imported bindings; unrelated source samples do not participate.

`inputs()` returns known source texts and structured `Unresolved` facts with
the owning path, launch line and reason. `import_facts()` parses each source
with the shared clause normalizer and recognizes literal `runpy.run_module`,
`importlib.import_module` and `__import__` targets through their imported
bindings. Invalid source, synthetic relative imports, dynamic targets and
unsupported source construction retain incomplete evidence.

This module does not execute code, evaluate builders, follow cross-file helper
contracts or claim to model all Python launches. Its consumer must retain
`unresolved`: an empty set of known dependencies cannot certify that an
execution input has no first-party dependency.

A raw dependency LCA is a placement fact; it does not establish private access
authorization. Completeness and the existing private policy have separate
consumer contracts.

`placement.collect_reference_evidence()` is the common collector for executed
inputs and ordinary first-party references. Its refs-only API,
`collect_references()`, raises `IncompleteReferenceEvidenceError` with the
retained facts when execution inputs are unresolved. `place()` also requires
complete subject references; it cannot silently certify a lower home from gaps.

The existing patch gate temporarily calls `legacy_patch_placement()`. This
adapter uses the same collector and subject calculation, pairs its inferred
home with the evidence, and is only for that legacy consumer. Its result is
explicitly marked `legacy-inference`; stderr and the census retain launch-site
gaps. New LCA consumers must use `ReferenceEvidence` directly, never this adapter.

`scripts/lint/patch_targets.py --strict-evidence` is a separate completeness
diagnostic. It retains the existing private policy and exits 1 for unresolved
execution inputs or existing-policy violations. `--report` does not suppress
this diagnostic failure. Normal hooks retain the same private policy and show
their evidence gaps. A root LCA already established from known subjects may
still retain unknown inputs in the independent locality analysis; completeness
failure is not a different placement conclusion.

The root integration owner coordinates the private-policy design discussion.
This contribution does not select a replacement authority rule. Remove the
legacy adapter only after that design is resolved, its real consumers and
private seams are reconciled, and the relevant tests and full-tree checks pass
without exemptions. The test-placement owner independently closes raw-LCA and
fixture/lane migration; a locality pass does not approve private authorization.

`style.py` supplies a pure rule for qualified imports in the coordinated
import-style migration.
Imports inside one repository top-level Python package use explicit relative
syntax, including nested sibling packages. Cross-package, stdlib and third-party
imports use absolute syntax. The rule consumes the normalized clause and the
original AST level, so style does not change architectural dependency targets.
Ruff keeps its banned-import checks and no longer selects TID252's preference
for absolute parent imports, which conflicts with this package boundary.

The rule is not connected to an active hook or CI gate. Enforcement must ship
with the consumer migration, without a baseline or package exception list.
Direct-script invocations, copied skill bundles and bare sibling imports need
their real loader contracts closed before that rollout. An empty `errors()`
result is not a compliance verdict for unresolved bare imports or independent
loaders. They must participate in the eventual gate through their actual package
and distribution ownership. No relative/absolute fallback is supplied. This
rule inspects actual import statements; dynamic
import strings, patch strings and non-executed source samples are separate
collector evidence. It preserves alias facts and diagnoses rather than applying
an automatic rewrite that could change which object a local name binds.

Import-linter contracts, private package doors and test-placement rules remain
responsible for their existing boundaries.
