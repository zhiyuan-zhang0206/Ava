# One cluster per host; every verification cluster runs in its own boundary

**Decision (2026-09-30, user ruling):** a host runs at most one Ava cluster, the
one at `~/.ava`. Nothing in the repository exists to keep two clusters apart on
one host. A cluster that exists to verify something (a preview, a rehearsal, a
proof) runs inside its own isolation boundary, a Linux container or VM, or a
Tart macOS VM, where it is that boundary's one cluster. The mechanisms below are
deleted, not deprecated. The sliced plan is
[one-cluster-per-host](../../future/infra/one-cluster-per-host.md).

## Context

Until now a cluster's identity was its home path and the code kept several homes
apart on one OS user: a port block allocated and preflighted per home, a
checkout-to-home pointer with an env-versus-pointer contradiction check, a bare
`ava` launcher that routes on `AVA_HOME`, a host-state directory shared across
homes, per-home OS job labels, and a local preview controller that birthed a
disposable cluster from a worktree. Each of the incidents recorded in
[path-only cluster identity](2026-07-20-path-only-cluster-identity.md) and
[AVA_HOME vs checkout contradiction](2026-07-31-ava-home-vs-checkout-contradiction.md)
was the same shape: "which cluster does this process mean" resolved through a
name, a pointer or an inherited variable on a shared host, and resolved wrongly.

Two proposals made in this line were patches inside that premise and are
rejected below: an `AVA_HOME` default exported from the user's shell rc, and a
default home built into the launcher. Two more concern how the home reaches a
process once the premise is gone.

## Why the premise goes, not the patches

Two versions of one tool sharing one PATH is an old problem with three standard
answers: install one, give every consumer its own prefix, or give every consumer
its own container or VM. `AVA_HOME` is the second answer. It works only while the
selector survives every layer between the operator and the process, and shells
rebuild PATH at every layer, which is exactly the shadowing the prefix approach
has to fight. The third answer moves the fight out of the code: a cluster in a
container or VM has its own PATH, ports, network and filesystem, and the
production home and credentials do not exist there to be reached by mistake.

What the repository itself says about who needs same-host coexistence:

- The local preview README scopes native previews to "source startup and real
  agent execution" and excludes browser and computer permissions, real provider
  behavior, multi-machine coordination and production cutover.
- The Linux lifecycle proof already requires an isolated Linux machine.
- CI gives every job its own throwaway cluster and tests give every session its
  own temporary `AVA_HOME`; neither needs the routing, pointer or registry
  machinery.
- The development loop in the self-development skill does not require a cluster
  per worktree; when local clusters are forbidden it uses selected tests and CI.
- The preview tooling is 21 tracked files with a single importer outside itself
  (a lint allowlist).

## What is deleted

Port-block allocation and cross-cluster port preflight; the `.ava_home` pointer,
checkout binding and contradiction check; `AVA_HOME_OVERRIDE`; the private
scratch home an unanchored checkout boots on; `ava start --worktree`; the
launcher routing and the per-home CLI link; the separate host-state directory;
per-home OS job slugs; the local native preview controller.

## `AVA_HOME` after the premise goes (user ruling, same day)

The home is `~/.ava` unless `AVA_HOME` says otherwise. Production does not
depend on the variable. Only a process tree that must not touch the host's
cluster sets it, once, at its top: the test session before it spawns anything,
and the hooks and tools that import application code. Every descendant inherits
it. No code captures the home at import; `ava_home()` reads it when called, and
there is no second, in-process injection channel, so a process and its children
cannot disagree about their home.

`AVA_HOME` no longer selects between clusters on a host, so the shell layers that
defeated it as a selector never stand between the setter and the process: the
test session sets it for the tree it owns.

What keeps development code off the host's cluster is now two rules. A test tree
always sets `AVA_HOME`; and the `~/.ava` cluster is started and stopped only from
its own checkout, `~/.ava/source`.

## Rejected alternatives

- **Keep same-host multi-cluster and export a default `AVA_HOME` from converge.**
  Depends on which dotfile a given shell form reads, must be written
  set-if-unset or it overwrites the `AVA_HOME` injected into an agent's login
  shell, and makes every worktree CLI refuse through the contradiction check.
- **A default home declared by the launcher.** Removes the dotfile dependence but
  keeps the routing machinery alive for a premise being retired.
- **A second OS user as the boundary.** A separate user has its own privacy
  grants, but ports are host-wide, so the port-block machinery would have to stay.
- **A `--home` argument instead of the variable.** It carries the same value, but
  every hop must forward it by hand, and a hop that forgets falls back to the
  default home, which is the host's cluster. The variable is inherited without
  anyone forwarding it; it is lost only where a spawner deliberately builds an
  empty environment.
- **Dependency injection of the home inside the process, with the variable only
  for children.** Two sources for one value can disagree between a process and
  the children it spawns.

## Consequences and what is not yet known

- A development checkout with `AVA_HOME` unset now reads the host's cluster;
  on a development machine that also runs production, that is production.
  Nothing it runs from there may start or stop that cluster.
- Native local previews disappear before their replacement exists. Until the
  container and Tart recipes land, verification is CI, the throwaway test
  clusters and the Linux lifecycle proof.
- macOS-native surfaces (the signed permissions helper, launchd custody,
  desktop capabilities) cannot run in a container. They need a Tart macOS VM,
  capped at two concurrent macOS VMs per Apple host by the virtualization
  framework and the license, or a dedicated machine.
- The helper signs with a self-signed identity created per keychain, which is why
  a rebuilt helper keeps its desktop grants; a fresh VM disk or OS user gets a new
  identity and needs one fresh approval. That a clone of a granted base image
  inherits both the identity and the grants is inferred from where they are
  stored, not yet tested.
- Not yet checked: whether the helper chain starts in a fresh macOS VM with no
  extra approval; whether any agent workflow still depends on a worktree cluster;
  whether the impersonation preview can run in a container.
