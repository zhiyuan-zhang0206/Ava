# Automated checks and judgment

A lint belongs in the contribution checks when detection is fast, offline,
deterministic and effectively free of false positives, and the author can fix
a violation within the current change. A test belongs in CI when it requires
an environment or toolchain unavailable to a normal commit hook. Both must
fail for a real violation rather than report success when nothing ran.

Debt requiring network data, history, semantic interpretation or a broader
repair belongs in [technical-debt guidance](tech-debt.md), not a mandatory
sweep ceremony. Contributors review relevant consequences of their changes;
a requested audit can inspect a wider scope using the same rules.

- Guard the deterministic core and leave judgment manual. Silent exception
  handlers belong to Ruff S110 and the diagnostics lint; legitimate defaults
  and wildcard matches need context. Skill-description hard limits belong to
  the description lint; shortening valid wording needs judgment.
- Reuse the automated owner's scanner and unit measure when inspecting a soft
  zone. Do not keep a second scope list that drifts independently.
- A one-time cleanup can be broad while the steady-state fix is local. Treat
  that cleanup as a bounded migration rather than weakening the gate.
- Existing lint or CI coverage replaces duplicate debt scans. Conflict markers
  are guarded by `check-merge-conflict` in hooks and structural CI, including
  rebased commits; another grep does not add a new rule.
- Introduced defects belong in the change that causes them. For a broader issue
  outside scope, preserve evidence in the single [debt ledger](../../../future/tech-debt/ledger.md).

A gate that fails for the wrong environment teaches contributors to disable
checks. Keep environment-dependent migration smoke tests in CI, where their
native toolchain is available. A broken hook can be skipped narrowly with
`SKIP=<hook-id>` only with a concrete explanation and remaining verification;
that is not permission to skip an actual violation or disable every check.

Likewise, a gate that cannot fail supplies false evidence. Before adding one,
identify the observable violation and a regression that fails without the fix.
