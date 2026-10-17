---
type: doc
title: Boot contracts
description: Current implementation constraints and their executable evidence.
---

# Boot contracts

```meiva-contract
id = "cold-sdk-entry"
kind = "invariant"
statement = """
The SDK entry completes its own namespace imports before allowing lazy plugin loading.
A bare non-agent import leaves context unbound and does not import database, Redis,
HTTP or model connection stacks. Process clients retain their supplied configuration
and build connections only on first use; installation clients outlive an attachment,
while bound calls retain the attachment's own producer. A reload must preserve the
first-loaded code image and process-context slots instead of relabeling or clearing
already-running code.
"""
code = [
  "meiva/boot.py:<module>",
]
tests = [
  "meiva/tests/bootstrap/test_sdk_import_initialization.py::test_non_agent_import_keeps_existing_unbound_context_contract",
  "meiva/tests/bootstrap/test_lazy_boot_import.py::test_import_meiva_clean_does_not_load_the_connection_stack",
  "base/agents/context/tests/test_clients.py::test_building_a_context_imports_no_connection_stack",
  "tests/components/meiva/test_sdk_event_producer.py::test_first_bound_plugin_load_does_not_lend_attachment_writer_to_bare_calls",
  "meiva/tests/namespaces/sdk_surface/test_process_clients.py::test_process_boot_builds_no_connection_stack",
]
```

Parent: [[meiva/docs/meiva.meiva.okf.md]].

The installation retains its own cold clients until process exit; a bound attachment uses its own producer. Identity binding precedes plugin installation. Surface loading and later runtime-face loading each retain their attempted/failed state: unknown configuration, inventory and I/O failures propagate without retry, while typed declaration refusals are isolated after rollback. An importlib fromlist probe cannot start loading against a half-imported SDK; the entry completion flag and in-flight loader slot are separate guards. Missing private names never start a load.
