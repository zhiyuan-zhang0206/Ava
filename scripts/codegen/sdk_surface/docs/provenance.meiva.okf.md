---
type: doc
title: Static SDK declaration and help provenance
description: Separate definition identity from the source declaration that supplies SDK help.
tags: [scripts, sdk, documentation]
---

# Static SDK provenance

The query reads canonical SDK declarations without importing application modules
or executing plugin code. Availability describes a declaration, not whether a
process has installed a plugin or bound an execution context. The existing
[resolver overview](../README.md) describes the supported syntax and boundaries.

The SDK query follows canonical core and plugin declarations without executing the inspected source. Proofs retain the implementing definition and declaration location; runtime-only paths remain unknown, and plugin availability remains conditional on installation.

## Module properties and help

Runtime lookup and help can have different source owners. `meiva.context` is served
by the module class's property, while `meiva.help` reads its module-level annotation
and adjacent PEP 224 text. `MemberProof.definition_name` and `source_line` retain
the getter identity. `documentation_name` identifies the help definition in the
same source file; ordinary definitions retain their existing name there.

The documentation owner is metadata about an already proved SDK member. It does
not make arbitrary annotation-only attributes into public APIs. The comment
checker consumes this owner for projection; it does not relax its ordinary
ambiguous-symbol rules for repeated getter, setter and deleter definitions.

Parent: [Static SDK declaration provenance](../README.md).
