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

```meiva-contract
id = "static-declarations"
kind = "invariant"
statement = "The SDK query follows canonical core and plugin declarations without executing the inspected source. Proofs retain the implementing definition and declaration location; runtime-only paths remain unknown, and plugin availability remains conditional on installation."
code = ["scripts/codegen/sdk_surface/contracts.py:<module>", "scripts/codegen/sdk_surface/contracts.py:query"]
tests = ["scripts/codegen/sdk_surface/tests/test_contracts.py::test_aliases_preserve_the_actual_definition_and_marker_locations", "scripts/codegen/sdk_surface/tests/test_contracts.py::test_plugin_member_declaration_proves_an_imported_callable_owner", "scripts/codegen/sdk_surface/tests/test_contracts.py::test_current_runtime_only_surfaces_remain_unknown"]
```

```meiva-contract
id = "canonical-markers"
kind = "invariant"
statement = "A canonical SDK marker must be one unchanged literal list of valid names. Missing, opaque, rebound or locally mutated declarations do not grant a fallback proof, and private names remain hidden."
code = ["scripts/codegen/sdk_surface/contracts.py:declared_names"]
tests = ["scripts/codegen/sdk_surface/tests/test_contracts.py::test_missing_opaque_or_mutated_markers_do_not_grant_fallback", "scripts/codegen/sdk_surface/tests/test_contracts.py::test_private_marker_names_stay_hidden"]
```

## Module properties and help

Runtime lookup and help can have different source owners. `meiva.context` is served
by the module class's property, while `meiva.help` reads its module-level annotation
and adjacent PEP 224 text. `MemberProof.definition_name` and `source_line` retain
the getter identity. `documentation_name` identifies the help definition in the
same source file; ordinary definitions retain their existing name there.

```meiva-contract
id = "property-help-owner"
kind = "invariant"
statement = "Only a statically proved ModuleType property with one unconditional module-level annotation without a value can use that annotation as its documentation owner. This does not replace getter provenance, authorize dynamic children, or repair a replaced property or ambiguous declaration."
code = ["scripts/codegen/sdk_surface/contracts.py:_property_documentation_name"]
tests = ["scripts/codegen/sdk_surface/tests/test_contracts.py::test_property_annotation_owns_help_without_replacing_getter_provenance", "scripts/codegen/sdk_surface/tests/test_contracts.py::test_annotation_does_not_repair_an_unproved_property", "scripts/codegen/sdk_surface/tests/test_contracts.py::test_current_context_help_is_owned_by_the_module_annotation", "scripts/codegen/sdk_surface/tests/test_contracts.py::test_module_property_provenance_does_not_grant_dynamic_children"]
```

The documentation owner is metadata about an already proved SDK member. It does
not make arbitrary annotation-only attributes into public APIs. The comment
checker consumes this owner for projection; it does not relax its ordinary
ambiguous-symbol rules for repeated getter, setter and deleter definitions.

Parent: [Static SDK declaration provenance](../README.md).
