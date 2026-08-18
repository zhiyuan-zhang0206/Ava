# Security model: deployment-host isolation, not code-execution sandboxing

## Context

An open-source-readiness audit caught a direct contradiction in `SECURITY.md`:
it claimed "Ava runs untrusted, model-authored code in a sandbox," while
[`future/roadmap/docker-sandbox.md`](../future/roadmap/docker-sandbox.md)
states plainly that execution lands directly on the bare host today ("Today
`execute_code` lands directly in the agent process and `ava.shell` subprocesses
spawn straight onto the bare host — no isolation"), and
[`conventions/non-goals.md`](../conventions/non-goals.md) already lists
"Sandbox" as a V1 non-goal. A security policy that misdescribes the actual
boundary is actively harmful: a vulnerability report, or a user's own
deployment decision, can rely on a containment property that doesn't exist.

The fix is not just deleting a wrong sentence — it needs a durable, positive
statement of what actually stands between model-authored code and the host, so
`SECURITY.md`, `non-goals.md`, and `README.md` describe the same boundary
instead of each drifting toward re-asserting a sandbox that isn't there.

## Decision

Ava does not sandbox model-authored code. `execute_code` runs the agent's
generated Python in the agent process, on the host, with the permissions of
whichever user started that process — the same trust model as a human running
that code themselves. The `before_exec` hook (`demos/permission-hooks/`) is a
mitigation layer (pattern-matching on dangerous commands before they run), not
a security boundary; `ava/security.py`'s own docstring says as much for its
prompt-injection scanning ("This is a mitigation layer, not a boundary").

The actual security boundary is **where you deploy the cluster**: a dedicated
OS user, machine, or VM per cluster. Ava already gives this some structure —
each cluster's identity is its own `$AVA_HOME`, and no two clusters share a
Postgres/Redis instance or a filesystem — but that is isolation *between*
clusters, not a boundary *around* any single agent's code inside one. If a
deployment needs a boundary the model's code cannot cross (untrusted input,
unattended automation, a blast radius smaller than "this machine"), that has
to come from a container, VM, or micro-VM wrapped around the whole cluster —
supplied by the operator, not by Ava.

This mirrors the stance pi.dev takes for the same reason: "Pi does not include
a built-in sandbox... [r]eal isolation needs to come from the operating system
or a virtualization/container boundary."

## Alternatives rejected

- **Ship a partial in-process sandbox** (restricted globals, a `RestrictedPython`-
  style AST filter, a seccomp profile scoped to the agent process). Rejected: a
  partial sandbox creates false confidence while the actual escape surface
  (filesystem, subprocess, network, package installation) stays wide open — the
  same reasoning pi.dev gives for skipping one. Ava's agent needs unrestricted
  shell, filesystem, and network access to do real work; a sandbox narrow
  enough to be a real boundary would also break the product.
- **Keep "sandbox" in `SECURITY.md` as a rough/aspirational description.** Rejected:
  the audit already caught the contradiction against `docker-sandbox.md`'s own
  admission that execution is bare-host today. Aspirational wording in a
  *security policy* specifically — the document a reporter or an integrator
  reads to decide what's safe to assume — is the one place vagueness is not
  affordable.
- **Hold the docs fix until the disposable-container work lands.** Rejected:
  that work ([`roadmap/docker-sandbox.md`](../future/roadmap/docker-sandbox.md))
  is scoped to a throwaway *cluster* substrate for eval / self-code-evolution
  workloads, not a general per-agent sandbox around ordinary `execute_code`
  calls (see the companion non-goals entry). It doesn't change what protects a
  normal deployment today, so there's no reason to hold the truth fix on it.

## Consequences

- `SECURITY.md`, `conventions/non-goals.md`, and `README.md` now describe
  one boundary — deployment host/VM/user, not in-process sandboxing. A future
  doc that reintroduces "Ava sandboxes `execute_code`" is wrong and should be
  corrected against this record.
- Operators who need isolation between untrusted input (or unattended
  automation) and their host must supply their own container/VM boundary —
  Ava does not provide one today, and the security policy says so.
- The `docker-sandbox.md` roadmap item stays scoped to the eval /
  self-code-evolution substrate; this record does not retroactively promote it
  to "the general security boundary."
