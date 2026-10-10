# File privacy and explicit component contracts

Status: accepted design; repository migration and gate activation remain open.

A leading underscore identifies a file-local implementation name or module.
Another file cannot import, read, or replace it, including a test or another
file in the same component. Renaming an implementation helper is not evidence
that it became a supported API. Consumer migrations must expose the owner's
actual capability, accept an explicit dependency, or test public behavior.

A non-private spelling permits communication between files in one component.
Communication across components additionally requires an exact entry module
and a member in the definition owner's static `__all__`. An entry is an actual
definition owner, not an initializer that re-exports somebody else's symbols.
No new barrels, compatibility aliases, baseline sites or consumer exceptions
can grant access.

## One owner for each fact

`pyproject.toml` declares responsibility prefixes and exact entry modules:

```toml
[[tool.ava.public_contracts.components]]
module = "base.db"
entry_modules = ["base.db.handle", "base.db.transaction"]
```

These paths illustrate the schema, not a requirement to move `Database` into
another module. The actual owner defines its interface in one literal list or
tuple `__all__`. The declaration contains no copy of those member names.
Wildcard entries and imported-member re-exports are invalid.

`base`, `services`, `cli` and the other large roots are containers, not a
single component whose descendants inherit access. Responsibility subtrees
such as `base.db` are declared explicitly. The longest declared prefix owns a
module. A separately declared child has its own boundary and does not inherit
the parent's internal privileges. An unclassified boundary fails rather than
becoming public by default. Existing import direction contracts still apply.

`scripts.structure` owns repository structural evidence and placement support;
`scripts.lint` owns policy checkers. The import grammar, placement, cochange and
public-contract checker retain their independently declared child boundaries.
Patch points, scan scope, service units and placement result models are exposed
from their existing definition owners. Consumers read canonical `CODE_TOPS` and
placement result types instead of forwarding aliases in the placement module.
These declarations resolve ownership, not private-access debt: a declared
parent still cannot grant private access or another child's entry.

The shared import grammar and checkout module index remain the owners of
relative imports, lexical aliases, literal dynamic imports and recognized
Python execution inputs. A module-level dependency fact does not prove the
definition owner of a re-exported member. Unsupported recognized inputs retain
diagnostics; the checker does not execute imports or interpret arbitrary Python.

Python's documented language metadata and data-model protocol names are
distinguished by their exact names, following the
[Python 3.12 data model](https://docs.python.org/3.12/reference/datamodel.html).
An arbitrary double-underscore name is still private. Framework or library
metadata is not automatically a language protocol. Accessing `__dict__` cannot
launder a known private key into a public member.

## Agent-facing SDK surface

The existing `ava.*` metadata, Installation and namespace generators own the
agent-facing surface. A generic contract must consume their authoritative
derived facts and definition provenance instead of introducing a second
hand-maintained export list. Static `__all_for_ava__` and dynamic namespaces
need that explicit integration; their spelling does not exempt private access.
This integration is still open at the migration stage.

## Delivery boundary

`scripts/lint/public_contracts.py` is a failing migration audit with meaningful
synthetic contract tests. It has no suppression or baseline switch. Initial
component declarations cover migrated leaf capabilities, not the entire tree.
The existing gates have not been weakened. Repository-wide component ownership,
consumer migration, recognized unknowns and the existing private/patch gates
must be reconciled before hook activation. An inactive checker or a clean leaf
slice is not completion of the public-contract capability.
