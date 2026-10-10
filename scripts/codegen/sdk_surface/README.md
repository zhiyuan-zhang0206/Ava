# Static SDK declaration provenance

`contracts.py` owns a read-only query over the existing SDK declarations. It
consumes the caller's checkout `ModuleSourceLookup`, the shared lexical import
bindings, module `__all_for_ava__` markers, and canonical `PluginContributions`
constructors in built-in `plugin.py` files. It does not maintain another member
list or import application modules.

`query(index, "ava.namespace.member")` returns `MemberProof` or `Unknown`.
Invalid query syntax raises `ValueError`. A proof retains the exact exposed
path, actual definition module/name, source location, declaring marker or plugin
contribution location, and availability. Definition names retain private
spelling: for example, the `ava.memory.search` declaration leads to `_search`,
not to a fictional public definition named `search`. A namespace definition has
an empty `definition_name` and its source begins at line 1.

Availability describes declarations, not the live process:

- `STATIC` means a core source declaration. It does not promise that a context
  is bound or that SDK configuration enables the path.
- `PLUGIN` means a conditional built-in plugin declaration. Installation,
  rollback, SDK-disable settings and wrapping remain runtime responsibilities.
- `UNKNOWN` means static provenance or canonical visibility was not proved.

Every traversed module namespace must have a literal canonical marker. Missing,
opaque, rebound or recognized locally mutated markers retain `Unknown`; the
runtime discovery fallback cannot certify a static interface. Private marker
names stay hidden. Module aliases and re-exports lead to their real definition
owners, and a package attribute cannot become a module just because a similarly
named source file exists. Rebound, conditional-only, cyclic or missing definition
bindings do not grant a proof.

Plugin namespace children always traverse the target module's markers, including
one-level children. A `SdkMember` is proved by its contribution declaration rather
than by membership in the host's initial marker: installation appends it. Its
host must be a declared core module namespace with a canonical marker, and the
target must resolve to a statically defined callable. Definite core namespace or
host member conflicts and multiple competing plugin declarations retain
`Unknown`.

The supported plugin syntax is a direct `PluginContributions(...)` return from
`contribute()`, containing literal tuple/list constructor calls. Constructor
aliases and positional/keyword arguments use the canonical imported type
identity. Arbitrary builders, generated contribution collections, third-party
plugin installations, `SimpleNamespace` factories, members hosted by another
plugin's namespace, class attribute traversal, live skill trees and MCP tools
are not evaluated. A statically declared module-class property can provide its getter's source
provenance; its returned object's members remain unknown. Dynamic `__getattr__`
values such as `ava.memory.PATH` also remain unknown.

These facts are input for a later explicit public-contract integration. A proof
for an exact SDK exposure grants no blanket `ava` exemption, direct private
access, general Python component entry or runtime availability claim. The
public-contract checker and component manifest remain separate policy owners.

Validation lives in `scripts/codegen/tests/test_sdk_surface_contracts.py`, with
synthetic positive/negative contracts and current-checkout provenance checks.
