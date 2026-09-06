# Distribution form: one core, packaging is a gated outer layer

How Ava reaches its different audiences: who gets Ava how, and what each path
actually needs.

## The core does not fork

There is one core (the cluster: gateway + agent-runner + agents + Postgres /
Redis + the macOS permissions helper). The only real variable across deployments is
**where the gateway runs** — and that is already a config knob, not a code
branch:

- **single box** — `gateway,agent-runner` both local (`~/.ava`).
- **split** — gateway on one host, an `agent-runner` satellite enrolled to it
  (`ava enroll`). The user's own multi-machine op (a cloud/server gateway + a
  Mac satellite that carries the desktop-bound skills) is this mode.

The user always interacts through the browser UI the gateway serves; "where the
core lives" only changes the URL (localhost vs a remote/tunnel address).

## Three audiences, three paths

| Audience | How they get + run it | Needs Apple Developer ID / a packaged `.app`? |
|---|---|---|
| **The operator's own machines** | existing helper + split deployment; grant TCC once per machine | **No** |
| **Open-source self-hosters (devs)** | `git clone` -> `install.sh` (prereqs + locked dependencies) -> `ava start` -> browser; grant TCC once on macOS | **No** |
| **Non-technical end users** | a signed, notarized, dependency-bundled one-click `.app` | **Yes** |

The decisive fact: **publishing the source is not shipping an installer.** A
self-hoster clones and runs the documented bring-up; on macOS the permissions helper's
`lifecycle.py` compiles + self-signs locally on *their* machine, so there is no
download quarantine and no Apple account in the loop. Code signing for TCC is
free (a stable self-signed cert — the property that survives rebuilds is the
designated requirement, not a paid identity). See the permissions helper
(`services/native/`) and [`decentralized-install-and-config.md`](infra/decentralized-install-and-config.md)
(install is a local operation).

## What the `.app` is — and when it is worth it

The signed `.app` is a thin wrapper that does not touch the core: it carries an
Apple Developer ID signature (so Gatekeeper + TCC are frictionless on a machine
the user never built it on), bundles the runtime deps (Postgres / Redis / Python
/ Chrome) so there is no `brew` step, and ships default flags + a menu-bar
launcher (single-box local, auto-open the browser to localhost). It is the
**only** path that needs the paid Developer ID + notarization, because it is the
only one crossing the download-quarantine boundary to people who will not clone a
repo.

**Gated / deferred:** build the `.app` when reaching non-technical adopters is an
actual goal, not before. Until then it is pure YAGNI — the operator's own use and
open-source self-hosting both need neither it nor the paid identity.

## So the open-source usability work is not packaging

What makes open-source adoption smooth is not signing or bundling, it is *can a
stranger clone and run*:

- `install.sh` prereq install clean for a newcomer (it grew around one operator's
  environment).
- A clear first-run path for the LLM API key + config.
- README must state the hard macOS constraint for desktop skills: a live,
  **unlocked** GUI session (synthetic input / capture are dropped on a locked
  screen).
- Windows ships the agent-runner half only; a Windows box cannot be a
  self-contained install ([`../gateway/windows-gateway.md`](../gateway/windows-gateway.md)).
