# CI secret scanning

## Decision

Use Gitleaks as the repository's single secret-scanning policy and CI gate.
The policy extends Gitleaks defaults, carries forward the development and test
fixture exceptions from the unused GitGuardian configuration, and also drives
the local pre-commit hook. The GitGuardian configuration was removed rather
than leaving a second, uncalled policy source.

The CI job scans complete Git history and the working tree. Its historical
baseline is limited to the seven commits that introduced known test, example,
demo, or planning findings; later commits containing the same material still
fail the scan.

Dependency auditing is introduced as an informational CI job. It executes
`uv audit --frozen` and `npm audit`, but is marked non-blocking while the
project establishes a remediation and enforcement policy. The canonical CI uv
version predates `uv audit`, so the job runs a fixed audit-capable uv through
`uvx` rather than changing the repository-wide toolchain pin.

## Consequences

- Secret scanning now runs on every eligible CI invocation, including
  documentation-only changes.
- Local and CI scans use the same allowlist rather than independently drifting
  private-key and GitGuardian rules.
- Existing dependency findings remain visible without making the initial audit
  rollout a delivery blocker.
