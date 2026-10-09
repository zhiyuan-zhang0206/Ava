# Static import ownership

`__init__.py` owns normalized clauses, package anchors, bindings and direct
dependency resolution. `cache.py` stores normalized production statements;
resolved module edges always use the current checkout.

`style.py` supplies a pure rule for qualified imports in the coordinated
import-style migration.
Imports inside one repository top-level Python package use explicit relative
syntax, including nested sibling packages. Cross-package, stdlib and third-party
imports use absolute syntax. The rule consumes the normalized clause and the
original AST level, so style does not change architectural dependency targets.

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
