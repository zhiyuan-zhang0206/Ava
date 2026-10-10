# Static import ownership

`__init__.py` owns normalized clauses, package anchors, bindings and direct
dependency resolution. Its `ModuleSourceLookup` protocol owns exact source
lookup and the checkout resource anchor; facts and mock proofs import it directly.
`cache.py` stores normalized production statements;
resolved module edges always use the current checkout.
Its public entry exposes `production_imports()` and the cache path. Consumers
compare cold and warm public results; cache tests observe source-file reads
without replacing the private parser. `lazy_modules.ModuleMap` is the existing
public lazy-index capability, with membership distinct from value construction.

`executed.py` supplies bounded facts about actual Python `-c` inputs. It follows
literal source, one plain binding and local undecorated helpers that pass a
source parameter unchanged or through one plain local binding. A positional
source before `*argv` and a keyword-only source remain independent of trailing
argument data. `**kwargs` and unpacking before a positional source stay unknown.
Literal `-W`/`-X` operands are consumed as interpreter options; source selection
stops at a script, `-m` or `--`. Launchers and Python executables must resolve
through their imported bindings; unrelated source samples do not participate.

`is_launcher(origin)` exposes the owner's existing lexical-origin predicate;
the launcher set remains private. A positive result does not prove a Python
executable or bounded source. `inputs()` and `module_input()` still perform
those checks, and callers must retain their unresolved evidence.

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

The approved component contract makes private names file-local, including
tests. The public-contract migration audit is not yet enabled as a repository
gate. Remove the legacy authority adapter only after its real consumers and
private seams are reconciled and complete checks pass without exemptions.
Test placement remains independent; a locality pass does not approve private
authorization.

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

`bindings.module_context()` lets a whole-module reader such as `facts.collect()`
resolve the module's own `__name__`, subscripts of literal tables and loop variables
over them. A table written through itself or a plain alias by subscript, augmented
assignment, a mutating method call or a `global`/`nonlocal` rebinding stays opaque.
Passing a table or its alias as a call argument also keeps it opaque without a
read-only proof; the collector does not execute the callee to infer its writes.
Iteration yields a dict's keys and indexing its values, resolved in the table's
definition scope. Like the one-binding rule, this does not observe writes from other
modules. Scopes built without a context keep the plain one-binding rules.

`bindings.local_nodes()` preserves lexical depth-first order with an explicit
iterator stack, so deep expressions do not repeatedly relay each node through
recursive generators. Nested bodies and definition-time inputs retain the same
scope boundaries.

Import-linter contracts, private package doors and test-placement rules remain
responsible for their existing boundaries.

`placement_evidence.subject_lca()` consumes the unpruned shared facts for root-test
admission, then removes replacement-only and test-support subjects without the
legacy all-patch fallback. It requires zero unknown inputs, including resource
gaps, and at least one resolved Python subject. Its exact-directory LCA does
not read empirical production edges or choose a unit by import direction.
