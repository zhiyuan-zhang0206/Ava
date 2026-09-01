# Exec child boot failure envelopes

## Decision

The exec child defers runtime and protocol imports until its entry guard has
the result path. A Settings or other boot failure is returned as a `crashed`
result envelope with its exception details and traceback. If protocol imports
or its writer are unavailable, a stdlib-only 0600 JSON writer preserves the
same parent-readable envelope shape.

Letting a disposable child terminate before it writes an envelope was rejected:
the parent can then report only a generic missing-envelope crash, hiding the
configuration error that prevents every `execute_code` call from starting.

## Consequences

- The parent surfaces boot-time configuration failures with their actual type,
  message, and traceback.
- The direct-child regression test covers the malformed PITR OSS configuration
  that triggered the incident and keeps healthy missing-request behavior fixed.
