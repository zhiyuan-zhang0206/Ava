# Security Policy

## Reporting a vulnerability

Report security issues **privately** — do not open a public issue. Use GitHub's
private vulnerability reporting: the repository's **Security** tab → **Report a
vulnerability**. Expect an acknowledgement within a few days.

Ava executes untrusted, model-authored code directly in the agent process, on
the host — there is no sandbox around `execute_code` — and it exposes an HTTP
gateway. So reports about **arbitrary code execution reaching beyond the
intended deployment boundary**, **gateway authentication**, or **data-plane
exposure** (Postgres/Redis reachability, the cluster secret) are especially
valuable.

## Security model

Ava does not sandbox model-authored code. `execute_code` runs the agent's
generated Python in the agent process, on the host, with the permissions of
whichever user started it — the same trust model as a human running that
code themselves. The `before_exec` hook (see
[`demos/permission-hooks/`](demos/permission-hooks/)) can intercept and warn on
dangerous patterns before they run, but it is a **mitigation layer, not a
security boundary**: it lowers the rate of a bad command executing, it does
not close off the possibility (see the module docstring in `ava/security.py`).

Third-party **skills** are the one ingestion path with a gate of its own, for the
same reason: a skill is text an agent is instructed to follow, so a malicious one
needs no exploit. Every package installed from outside this repo is read by
`shared/skill_scan.py` first, and a critical supply-chain pattern (a
download-and-execute pipeline, an obfuscated payload, a credential store read
paired with an outbound sink, instructions to work behind the user's back)
**refuses the install**. It is pattern matching, so the same sentence applies:
a clean report means "no rule matched", not "safe". Installed third-party
content stays at trust tier `unreviewed` until a person runs `ava skill trust`.

### Local privileged services

Beyond `execute_code`, several daemons hold capabilities a same-user process
could abuse, and none of them is individually authenticated (audit round-2
up-security-trust): the **permissions helper** Unix socket
(`services/permissions_helper/`) is owner-connect-only but has no token
handshake — it holds the macOS Screen Recording + Accessibility grants and
can capture the screen and inject clicks/keys; the **managed Chrome** CDP
port (`--remote-debugging-port`) is unauthenticated by design and the bridge
injects a configurable, server-side gateway session cookie into it; the **mcp
daemon** socket
shares the machine with the cluster secret. Their real boundary is the OS
user: only processes running as the same user can reach them, and that user
is the same trust domain `execute_code` runs in. Same-user isolation for
these is a non-goal today; if a deployment needs it, isolate the OS user or
run the cluster in a VM/container (below).

The isolation Ava relies on is **where you deploy the cluster**, not how it
executes code:

- Run each cluster under its own OS user, machine, or VM. A cluster's identity
  is its `$AVA_HOME`, and two clusters never share a Postgres/Redis instance —
  but that is deployment-topology isolation between clusters, not a code
  sandbox around any single agent's execution.
- Don't point an agent at credentials, filesystems, or networks it shouldn't be
  able to reach — the agent can reach anything the OS user it runs as can
  reach.
- Don't run Ava against untrusted third-party input (an agent ingesting
  attacker-controlled web content, files, or messages) without treating the
  whole host as exposed to whatever that content can make the agent execute.

If you need real isolation between the model's code and your host — untrusted
inputs, unattended automation, or a blast radius smaller than "this machine" —
run the cluster inside a container, VM, or micro-VM with only the credentials
and filesystem access the task needs. That boundary has to come from the OS or
a virtualization layer; nothing inside Ava provides it today. A disposable,
fully containerized cluster is on the roadmap
([`future/roadmap/docker-sandbox.md`](future/roadmap/docker-sandbox.md))
as the substrate for eval and self-code-evolution workloads specifically — it
is not a general-purpose per-agent sandbox, and the absence of one is a
tracked, deliberate non-goal
([`conventions/non-goals.md`](conventions/non-goals.md)).

## Supported versions

Ava is pre-1.0 and ships from `main`; fixes land on `main`. There is no backport
branch yet.
