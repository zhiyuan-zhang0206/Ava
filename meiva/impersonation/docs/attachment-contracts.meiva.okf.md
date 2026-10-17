---
type: doc
title: Attachment contracts
description: Current implementation constraints and their executable evidence.
---

# Attachment contracts

```meiva-contract
id = "attachment-capture-lifetime"
kind = "invariant"
statement = """
An attachment borrows a validated controller identity and restores the prior process
context on detach. Its own client set and admission gate are distinct from the SDK
installation's longer-lived cold producer. Each admitted SDK call retains its original
durable capture gate even when another attachment becomes current. Closing admission
and marking the attachment closing share one lock: already-admitted calls may drain,
later calls are refused. Close attempts plugin-state flush, receipt closure, telemetry
delivery and detach; a later cleanup error cannot replace the primary failure. A
still-running admitted call retains its original receipt and seals it only when it
actually drains; a finite close wait cannot certify that source empty.
"""
code = [
  "meiva/impersonation/attachment.py:<module>",
]
tests = [
  "agent/tests/test_external.py::test_repeated_close_cannot_release_another_attachment",
  "agent/tests/test_external.py::test_close_rejects_a_new_sdk_effect_before_it_reaches_the_gateway",
  "agent/tests/test_external.py::test_close_does_not_revoke_an_sdk_call_admitted_before_the_fence",
  "agent/tests/test_external.py::test_close_preserves_flush_error_when_receipt_cleanup_also_fails",
  "agent/tests/test_external.py::test_attachment_context_keeps_body_error_primary_when_close_fails",
  "meiva/tests/namespaces/sdk_surface/test_impersonation_capture_rebinding.py::test_concurrent_calls_capture_the_original_gate_after_context_rebind",
  "meiva/tests/namespaces/sdk_surface/test_impersonation_capture_rebinding.py::test_known_capture_database_outage_retains_the_failed_receipt_recovery",
  "tests/components/meiva/test_sdk_event_producer.py::test_first_bound_plugin_load_does_not_lend_attachment_writer_to_bare_calls",
]
```

Parent: [[meiva/impersonation/docs/attachment.meiva.okf.md]].

The attachment module loads on first controller use, so a bare SDK import does not load its kernel codec. The codec and snapshot reader remain the agent kernel owners. Native plugin faces must load before checkpoint state validation; surface-only loading would discard unregistered fields. Checkpoint state excludes journal replay, and already-receipted journal entries are not replayed. Framework state injection and full history replacement are refused by the shared codec. Ordinary SDK effects happen when called and are not rolled back by closing the attachment. Lease use rechecks controller ancestry and expiry; reading a previously loaded Python object does not renew or recheck the lease.
