# Verification boundaries: containers and Tart VMs in place of a local preview

**Status: the Linux container recipe is built and has run; the Tart recipe is an
experiment whose result is not in.** This is slice 6 of
[one cluster per host](one-cluster-per-host.md); the ruling and its reasons are in
[one cluster per host](../../decisions/2026-09-30-one-cluster-per-host.md). The native
local preview is deleted. This doc says what replaces it: which isolation boundaries
exist, what each one can prove, how a cluster is born and destroyed inside each, what
the container run showed, and which questions are still open.

Facts below are tagged where it matters: **known** (read in the code, a cited source,
or observed in a run), **inferred** (follows from known facts, not exercised),
**untested**.

## Decisions

User rulings on this slice (2026-10-01):

1. **The container recipe comes first.** It needs no new host software and no
   download beyond package installs, and it restores the one claim no CI job carries
   (a fresh `ava start` from source).
2. **The Tart experiment runs in parallel** with it; its result is not in. Until it is,
   the Tart sections below are design, not a recipe.
3. **Tart uses the `base` image** (SIP disabled), not `vanilla`.
4. **The model is scripted.** No verification boundary carries a provider key: the
   scripted scenario executes real code through the real gateway and agent host, and
   nothing secret is injected into an image, a container or a VM. Real provider
   behavior is therefore not a claim any boundary carries today (a real-model run
   would need its own decision and its own key-handling design).

## Boundary

A verification boundary is an OS environment created from a pinned image in which one
cluster is born from one immutable commit through the ordinary start entry, observed,
and destroyed. Inside it the cluster is the machine's one cluster: home `~/.ava`,
source checkout `~/.ava/source`, the fixed port table. Two kinds are in this slice:

| Boundary | Status | Carries |
|---|---|---|
| Linux container (Recipe A) | built, `scripts/verify/` | source start, real agent execution with a scripted model, frontend and CORS over loopback |
| Tart macOS VM (Recipe C) | experiment open | signed helper chain, launchd custody, desktop and headed-browser capabilities |

A Linux VM with systemd is a third form, used only when boot custody is the subject;
it exists in CI (see "Other Linux forms").

In scope: the image definitions, the per-run recipe, the observer, the one-time grant
procedure, the claim matrix. Out of scope: the start entry itself (slices 2 to 5),
multi-machine coordination (two boundaries plus a gateway URL; not designed here),
production cutover rehearsal against restored production data, and the logins the
impersonation hosts need.

## What every boundary guarantees

1. **One cluster, production layout.** Home `~/.ava`, source at `~/.ava/source`, so
   the rule "only `~/.ava/source` starts `~/.ava`" holds with no special case.
2. **Nothing of the host's cluster inside.** No mount of a host `~/.ava`, no container
   runtime socket, no SSH keys, no environment passed in, no provider key.
3. **Born from a pinned image and one commit.** The ref is resolved to a commit once;
   uncommitted files are not included; evidence names the commit and the image ID.
4. **Loopback stays loopback.** Nothing is published to the host. Observation runs
   inside the boundary.
5. **Destroyed after, evidence first.** Logs and the observer's JSON are copied out,
   then the container or VM is deleted. There is no "keep it for later" state.
6. **Not a sandbox against hostile code.** The boundary keeps a mistake away from the
   host's cluster; it is not a security boundary for untrusted branches.

## Claims each boundary can carry

| Claim | Container | Linux VM + systemd | Tart VM | Hosted CI today |
|---|---|---|---|---|
| Fresh home, source start through `ava start` | yes | yes | yes | no job (inferred from the workflow and test inventory) |
| Real agent execution (scripted model) | yes | yes | yes | harness processes, not the start entry |
| Real provider behavior | no (scripted model only) | no | no | no |
| Headless browser against the frontend | not built (Chromium is not in the image) | possible | possible | yes (e2e) |
| systemd boot custody | no | yes | no | yes (`native-root-lifetime-proof.yml`) |
| launchd -> helper -> root chain, stable identity | no | no | yes | ad-hoc signed only |
| Desktop grants, computer capability, headed Chrome | no | no | yes, once granted | no |

## Recipe A: Linux container

Run it from a development checkout with Docker available:

```
python3 scripts/verify/container.py --ref origin/main
```

The script is stdlib-only and host-side. It resolves `--ref` to one commit, builds the
image if its inputs changed, starts a container, runs the steps below, copies the
evidence into `<evidence-root>/<time>-<sha8>/` (default under the system temporary
directory), and removes the container with its volumes. Exit status is zero only when
every step and every observer check passed and the container is gone.

### Why a container is enough (known)

- **No systemd needed.** `cli/commands/lifecycle/root_driver.py` launches `ava-root`
  directly on every non-macOS platform (`_spawn_direct`). systemd is used only for
  automatic boot (`base/host/system/boot_unit.py`, a system-scope unit that needs root
  to install). With `AVA_OS_JOBS_ENABLED=0` (`base/host/system/cron.py:os_jobs_enabled`)
  converge registers no cron, boot or logs jobs.
- **Data plane is native inside the container**, as everywhere; this is the opposite of
  the retired compose data plane
  ([decision](../../decisions/2026-09-28-retire-windows-docker-compose.md)). Postgres
  keeps shared memory in mmap files under its data directory
  (`base/cluster/dataplane/pg_tools.py`).

### Image

`scripts/verify/Dockerfile`, base `ubuntu:24.04`. The image is the machine, not the
software under test:

- The system layer is the repository's own provisioning, not a second package list:
  `scripts/provision/install-system.sh` (Python 3.12, PostgreSQL 17 and pgvector,
  Redis 8.2, PgBouncer at the pinned pgdg build, Node 22, the CLI tools) and
  `scripts/provision/toolchain.sh` (uv at the pinned release).
- An ordinary user, `verify`, with a home: PostgreSQL refuses to run as root, and the
  data plane's administrator is the OS user over `peer` authentication on an
  owner-only socket. The user is **not** named `ava`: initdb makes the OS user the
  bootstrap superuser, and start refuses a superuser that shares its name with the
  schema owner role `ava` (found by the first run).
- **No source, no virtualenv, no `node_modules`, no secret** in the image. The build
  context is filtered to the provisioning scripts (`Dockerfile.dockerignore`). The image
  is tagged with a digest of those inputs, so it is built once and reused until they
  change; evidence records the image ID and the base image digest.
- One named volume, `ava-verify-cache`, holds the uv cache, the managed Python
  interpreters and the npm cache. It carries no cluster state and no secret.

The root `Dockerfile` that used to live in the repository was an orphan of the deleted
image workflow (it ran as root, installed Ubuntu's `redis-server`, and had neither
PgBouncer nor pgvector); it is replaced by this image rather than kept beside it.

### Run

One `docker run`: detached, `--init` (ava-root detaches into its own session and is
reparented to PID 1, which must reap), a memory limit and `/dev/shm` size (8 GB and 1 GB
by default), `--cap-drop ALL`, `no-new-privileges`, the cache volume, and exactly one
bind mount: the host repository's object store (`<git-common-dir>/objects`), read-only.
No published port, no `--network`, no `-e`, no container runtime socket, no SSH key, no
host `~/.ava`; `refuse_host_state` rejects the home directory, any parent of it, the host
cluster and credential directories, and any socket as a mount source. The working tree
is never mounted, so uncommitted files cannot enter, and the repository's `config` (which
may hold remote URLs) stays on the host.

Steps, each logged to its own file in the evidence directory:

1. `prepare-source`: `git init ~/.ava/source`, point its alternates at the mounted
   object store, `git checkout --detach <commit>`, `git repack -a -d`, drop the alternates.
   The result is an ordinary standalone clone, which the start admission needs (it
   hashes the tracked and non-ignored files); nothing depends on the mount afterwards.
2. `uv sync --locked` in the checkout; `npm ci` in `ui/web`.
3. Write the start profile and upload the observer.
4. `ava start --serve-gateway --serve-agent-runner --machine-name verify --machine-host
   127.0.0.1 --config-file <profile> --only-service gateway frontend ops agent-host`:
   the first start of a single box. `~/.ava` lives on the container's own filesystem,
   never a bind mount (unix sockets, peer authentication, fsync).
5. `observe`: the observer below.
6. Copy out `observer.json` and the cluster's logs; `docker rm -f -v`.

The start profile is the deleted controller's, plus a declared time zone:
`AVA_OS_JOBS_ENABLED=0`, `AVA_PROVISION_BUILTIN_SCHEDULES=0`, `AVA_BROWSER_ENABLED=0`,
`AVA_MEMORY_KEEP_LOCAL=1`, `AVA_CROSS_MACHINE_TRANSFER_BACKEND=none`,
`AVA_REQUIRE_GITHUB_PR=0`, `AVA_TELEMETRY_OTLP_ENABLED=0`, `AVA_TIMEZONE=UTC`, and
`AVA_LLM_OVERRIDE=tests.e2e.fakes.scenarios.message_flow:build` for the scripted
scenario. It contains no key of any kind. A branch under test can read everything in its
container, which is why nothing secret is put there: trusted branches only still applies
to what the branch can do with the machine, not to what it can steal.

### Observing

The gateway binds `127.0.0.1` without a cluster secret (`gateway/_server.py`) and the
frontend starts with `next start -H 127.0.0.1` (`ui/web/package.json`), so `docker run -p`
cannot reach either. A cluster secret would bind the gateway to all interfaces and
require an `AVA_TRANSPORT_ENCRYPTION` declaration. The browser dials
`<page hostname>:<gateway port>` (`base/cluster/derive.py:fe_build_env`), so a forward to
the host would have to reuse the fixed port numbers, which collide with the host's own
cluster. So the observer (`scripts/verify/observe.py`) runs inside the container, from the
checkout's interpreter, and writes `observer.json`. It runs every check and records each
outcome, so a partial result keeps its evidence:

| Check | Passes when |
|---|---|
| `toolchain` | Redis is on the approved 8.2 series, PostgreSQL is 17 with pgvector installed; records the versions of PostgreSQL, Redis, PgBouncer, Node, uv and Python |
| `source` | the checkout is at the named commit with no changed or untracked file (so no build step dirtied a tracked file) |
| `services` | gateway, frontend, ops and agent-host answer their identity probes (`ops.roster`) |
| `frontend` | the app port answers 2xx over HTTP |
| `cors` | the gateway answers the frontend's exact origin with credentials allowed and `authenticated: true` |
| `scripted_agent` | an agent created through the gateway executed `print(1 + 2)` through the real agent host and its recorded output body is exactly `3` |

The agent check judges the recorded execution body, never a digit in a timestamp or a
model reply. Nothing is forwarded to the host. A browser UI check would need Chromium in the image at
the Playwright version `uv.lock` pins (`scripts/provision/install-playwright.sh` still
pins an older one; see the tech-debt ledger) and a large enough `--shm-size`; it is not
built.

### Run results

### What the runs showed

Known, from runs of the recipe on `linux/arm64` under a local Docker engine (the project
commits no run, so this is what the runs established, not a log):

- **arm64 runs natively; no `linux/amd64` emulation is needed.** The vendored Postgres has
  no `linux/aarch64` artifact (`runtime_binaries._platform_key` raises), but
  `ensure_pg_runtime` validates an installed PostgreSQL 17 with pgvector first, and the
  pgdg, redis.io and nodesource repositories all serve arm64, as does the pinned
  PgBouncer build (`1.26.0-1.pgdg24.04+1`). Only the image's package install differs from
  an amd64 host.
- **`ava start` completes in a non-root, no-systemd, no-sudo container** (the image has no
  `sudo`; converge on Linux needs none with OS jobs off). Direct launch of `ava-root`
  works: the start reports the root spawned directly, and the four services, the
  per-cluster Postgres, Redis and PgBouncer all come up.
- **The scripted scenario runs through the real stack**: gateway, agent host, exec child,
  the recorded code `print(1 + 2)` and output body `3`.
- **Time.** Cold (no image layers for the current inputs, empty cache volume): image build
  about 2.5 minutes, `uv sync --locked` about 1 minute, `npm ci` about 1 minute, first start
  about 30 seconds (it includes the Next.js build), observer about 4 seconds. With the
  cache volume warm the two installs take seconds.
- **Memory.** The container's cgroup peak (page cache included) was 3.7 GB and 5.0 GB in
  two runs, dominated by the frontend build; the 8 GB default leaves headroom.
- **Size.** The image is about 1.6 GB on disk (380 MB of compressed content); the cache
  volume about 1.4 GB.
- **Source.** The recipe makes a full-history standalone clone (38 MB, about a second) and
  start admission accepts it. Whether admission would accept a depth-one fetch was not
  tried.
- The start prints one benign warning here: the checkout has no installed Git hooks.

What the first runs got wrong, now fixed in the recipe: the container user was named
`ava`, which start refuses (see Image); start also warned that no time zone was declared,
so the profile declares `AVA_TIMEZONE`.

### Redis on Linux

Linux Redis was unpinned: `database.sh` installed whatever `packages.redis.io` served
(8.10.2 when this recipe was written), CI installs Ubuntu's `redis-server` (7.0), and
macOS pins `redis@8.2`. AGENTS.md approves Redis 8.2. This slice pins the provisioning
script to the 8.2 series: `redis-server` and `redis-tools` at `6:8.2.*` (the newest 8.2
patch release redis.io serves; 8.2.10 at this writing), held with `apt-mark hold`, the
canonical value in `base/host/brew_pin.py` and a test that keeps the two copies equal.
The effect is on hosts provisioned with this script from now on; a host that already has
`redis-server` skips the install (the presence gate) and is unchanged. CI's install action
still takes Ubuntu's `redis-server`, so the fidelity this boundary claims does not extend
to the hosted runners.

### Other Linux forms

- **OrbStack or other Linux VM with systemd.** Only when boot custody is the subject.
  Create it isolated (`orb create --isolated`), or it shares the host's files and forwards
  its localhost ports to the Mac's. The custody claim is already carried by
  `native-root-lifetime-proof.yml` on a hosted VM; a local machine is for debugging that,
  not a second gate.
- **Hosted VM by `workflow_dispatch`.** Needs no image or local resources and reuses
  `.github/actions/install-pg-redis`; the branch must be pushed and there is no
  interactive inspection.

## Recipe C: Tart macOS VM

**Design; the experiment that decides its shape is open.**

### Prior art

[Tart](https://github.com/openai/tart) runs macOS and Linux VMs on Apple Silicon over
Virtualization.framework, distributes images as OCI artifacts, and clones by APFS
copy-on-write. Others run disposable computer-use VMs the same way: a golden image with
grants, clones per run
([jonnyzzz/tart-skills](https://github.com/jonnyzzz/tart-skills),
[agentuplink PR 177](https://github.com/andymac4182/agentuplink/pull/177),
[capsule](https://github.com/akira-toriyama/capsule),
[pilot-images](https://github.com/WeZZard/pilot-images); secondary evidence, their
clone-inheritance claims differ and none uses a self-signed helper). Writing a
virtualization layer directly would repeat Tart.

### Numbers (known, measured against registry manifests and releases on 2026-10-01)

- Tart release archive: 22.9 MB (2.40.1). Install: `brew install openai/tools/tart`.
- Images `ghcr.io/cirruslabs/macos-tahoe-{vanilla,base}`: layers total 24.0 GB and 27.3 GB
  compressed; logical disk 50 GB, sparse. On-disk size after pull is not measured. Clones
  are copy-on-write and claim space only as they diverge.
- `vanilla`: SIP on, auto-login, SSH, passwordless sudo, no guest agent. `base`: vanilla
  plus Homebrew, git, Node, the Tart guest agent (`tart exec`), **SIP disabled**, and
  pre-written TCC rows for ssh, osascript, python and the guest agent
  ([templates](https://github.com/cirruslabs/macos-image-templates/blob/main/scripts/update-tcc-database.sh)).
  Credentials `admin` / `admin`. **The decision is `base`** (see Decisions).
- Licensing: at most two macOS VMs per Apple host, from the macOS license (section
  2B(iii), "up to two (2) additional copies", purposes include software development and
  testing) and enforced by the kernel
  ([Tart discussion](https://github.com/cirruslabs/tart/discussions/1054),
  [kernel quota](https://khronokernel.com/macos/2023/08/08/AS-VM.html)). Whether Linux
  guests count toward that quota is not established by these sources. Tart itself is
  FSL-1.1-ALv2 (no fee; Competing Use excluded; converts to Apache-2.0 two years after
  each release). The Cirrus Labs team joined OpenAI in April 2026 and the dedicated team
  moved on to other work
  ([announcement coverage](https://macstadium.com/blog/cirrus-labs-is-joining-openai));
  releases and image rebuilds were still flowing in September 2026. Pin the Tart version
  and the image digest.
- Headless: from macOS 15 the host needs an unlocked `login.keychain` to start any VM
  ([FAQ](https://tart.run/faq/)); `tart run --no-graphics`, `tart exec` (guest agent),
  `tart run --dir=name:path:ro` (macOS guests mount it under
  `/Volumes/My Shared Files/name`).

### Golden image (one-time, user present)

From `macos-tahoe-base`: add the toolchain (`uv`, Node 22, `redis@8.2`, `pgbouncer`,
`pgvector`), then, with the same code the start path uses, create the signing identity
(`services.permissions_helper.lifecycle.ensure_signing_cert`), build and sign the helper,
load it, and grant Screen Recording and Accessibility in System Settings. Record the
identity's SHA-1, the designated requirement and the TCC rows. Leave no cluster home in
the golden image.

Grant routes: (1) click once in the golden image, then clone (the route under test); (2)
on the SIP-off `base`, write the rows into `TCC.db` with a code requirement compiled from
the helper's designated requirement (the Cirrus script does this for other clients; Ava's
own notes call direct writes unverified on macOS 26; the user database moves into a
per-user container in macOS 27); (3) MDM cannot grant Screen Recording, only deny it or
let a standard user approve
([PPPC overview](https://www.iru.com/blog/archive/changes-to-pppc-in-macos-big-sur)).

### Per run

`tart clone` the golden image; `tart run --no-graphics --dir=src:<repo>:ro`; `tart exec` to
unlock the guest login keychain (the signing probe refuses or hangs on a locked one),
clone the commit from the shared directory into `~/.ava/source` (local disk, not the
virtio-fs share), sync dependencies, start, observe, copy evidence out, stop and delete
the clone. Observation runs in the guest; a host browser pointed at a forwarded guest port
has the same port-number problem as in Recipe A. The container's observer is reusable in
the guest: it reads only the home and the checkout.

### Limits

Two macOS VMs at a time, golden image included while it runs. The helper's grants are keyed
to the bundle id and the certificate's designated requirement. The onboarding note records
that a same-identity rebuild kept the Screen Recording and folder grants, an identity change
drops them, and other notes say a rebuild resets Accessibility and some extended rows
(unknown 2). A rebuild is skipped when the source and the requirement hash are unchanged.

## Surfaces only macOS can verify, and what CI keeps

| Surface | Why macOS | Hosted macOS CI today | Tart VM |
|---|---|---|---|
| Helper signed with the stable identity | needs imported-identity codesign; hosted runners cannot (comment in `ci.yml`) | ad-hoc signature only | yes |
| launchd -> helper -> root -> services ancestry; keeper conflict and restart | launchd | yes (`scripts/ci/two_section_chain_smoke.py --skip-attribution`) | yes, plus tccd attribution |
| Desktop grants and their attribution | tccd and the consent UI | no | yes |
| Computer capability (capture, click, type) | grants, GUI session | no | yes |
| Headed Chrome and browser profile | macOS Chrome, GUI session | no | yes |
| macOS data plane (vendored PG, brew Redis and PgBouncer, SysV shm limits) | mac binaries | Linux-only pgvector smoke | yes |
| `ava start` end to end on macOS | helper refuses ad-hoc, no direct-spawn fallback | not possible without a test hook | the only place besides a real Mac |
| LaunchAgent jobs (health probe, logs, packages, boot) | launchd | no | yes |

Nothing above moves to the hosted runner without weakening the no-ad-hoc, no-fallback rule
in the helper, so they stay on the VM.

## Not yet known

Tart (the open experiment):

1. **Does a clone of a granted image keep the signing identity and the grants?** Inferred
   yes: a clone copies config, NVRAM and disk and regenerates only the MAC address
   (`VMDirectory.clone`), so the keychain file and both TCC databases are byte-identical.
   Untested for this helper.
2. Whether a helper rebuilt in a clone (fresh home, same identity) keeps Accessibility; the
   client's own diagnostic says a rebuild resets it once.
3. Whether the helper chain and signing work under `tart exec` and the auto-login session
   with no extra approval, and whether the keychain needs unlocking each boot.
4. Whether Screen Recording re-prompts on a schedule in a long-lived golden image (macOS 15
   prompts monthly per app; state lives in
   `~/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist`; macOS
   26 behavior not checked).
5. Whether Linux guests count toward the two-VM kernel quota, and whether the Linux
   container runtime's VM does.

Container:

6. Whether the impersonation preview can run in either boundary: it needs external host CLI
   logins, which would have to arrive as dedicated revocable credentials, and the scripted-
   model ruling means no secret enters a boundary today.
7. Whether the same recipe passes on `linux/amd64` (the architecture CI and most Linux hosts
   use): nothing in it is arm64-specific except the package install, but it has only been
   run on arm64.
8. A browser UI check (Chromium in the image) and real provider behavior are not carried;
   each needs its own decision.

## Experiment B: clone inheritance, outside the repository

Answers unknowns 1 to 4 and decides whether the Tart recipe is clone-per-run or
click-per-run. Runs in parallel with Recipe A; result not in.

1. Clone `macos-tahoe-base` to `ava-golden`; 4 CPUs, 8 GB.
2. In the golden image, with a scratch checkout: create the identity, build and load the
   helper, grant Screen Recording and Accessibility by hand. Record: identity SHA-1;
   `codesign -d -r-` output; TCC rows for `com.ava.permissions-helper` (user and system
   databases, including the code requirement blob); helper `ping` fields `preflight_screen`
   and `ax_trusted`; a real capture that is not uniform.
3. Take two clones of the golden image: R1 with the helper artifact present, R2 with `~/.ava`
   removed before cloning (first start rebuilds the helper). A third clone taken before the
   grants is the negative control.
4. Boot each headless, unlock the keychain, repeat the recording.
5. Stop and start R1 again; run R1 and R2 at once.
6. Pass: R1 and R2 report the same SHA-1 and requirement and both grants true with no dialog;
   control reports false. R1 only: bake the helper artifact outside the home. Neither: try
   route 2, else count the clicks per clone (two toggles and a password).
