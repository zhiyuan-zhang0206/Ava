# Static import ownership

`__init__.py` owns normalized clauses, package anchors, bindings and direct
dependency resolution. `cache.py` stores normalized production statements;
resolved module edges always use the current checkout.

`executed.py` supplies bounded facts about actual Python `-c` inputs. It follows
literal source, one plain binding and local undecorated helpers that pass a
source parameter unchanged. Launchers and Python executables must resolve
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

The existing placement and private-patch gates do not yet consume these facts.
Their integration must explicitly handle incomplete evidence before using a
dependency-derived home as private access authorization. A raw dependency LCA
is a placement fact; it does not establish that authorization.
