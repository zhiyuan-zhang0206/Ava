# Verification boundaries: containers and Tart VMs in place of a local preview

**Status: both recipes are built. The Linux container recipe has run; the Tart recipe
(`scripts/verify/tart_run.py`, `tart_golden.py`) has run once against a golden image
built by hand (see "Run results" under Recipe C), and the Tart clone-inheritance
experiment (Experiment B, below) has run. The golden-image build script has run only as
`--dry-run` and under test.** This is slice 6 of
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
2. **The Tart experiment runs in parallel** with it. Its result is in (Experiment B): a
   clone of a granted image keeps the helper's signing identity and both grants, so the
   Tart recipe is clone-per-run. The recipe is scripted (`scripts/verify/tart_run.py`) and
   one run of it carried a full `ava start` in a guest (Recipe C, "Run results").
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
| Tart macOS VM (Recipe C) | built, `scripts/verify/`; one real run recorded below | signed helper chain, launchd custody, desktop and headed-browser capabilities |

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
4. `ava init --serve-gateway --serve-agent-runner --machine-name verify --machine-host
   127.0.0.1 --config-file <profile>`, then `ava start --only-service gateway frontend ops
   agent-host`: the first start of a single box. `~/.ava` lives on the container's own filesystem,
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
the Playwright version `uv.lock` pins (`uv run playwright install chromium` + `install-deps`,
as CI does) and a large enough `--shm-size`; it is not built.

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

Run it from a development checkout on an Apple-silicon Mac that has Tart and a golden image
(built once, below):

```
python3 scripts/verify/tart_run.py --ref origin/main
```

The script is stdlib-only and host-side (`tart_run.py` over `tart_vm.py` and the shared
`boundary.py`, which the container recipe also uses for its start profile, argv, host-state
refusal and evidence steps). It resolves `--ref` to one commit and records, step by step in
`<evidence-root>/<time>-<sha8>/result.json` (one log file per step):

1. `export`: a fresh bare repository holding only that commit's history, in a temporary
   directory. It is built by a fetch, so no file in it is hard-linked into the host's object
   store (a share holding a `git clone --local` of the host repository failed in the guest:
   `unable to open loose object ... Permission denied`, while the fetched export passed
   `git fsck`) and the host repository's config and other branches stay out.
2. `clone`: `tart clone <golden> ava-verify-<sha8>-<random>`, from a golden image that exists
   locally and is stopped.
3. `boot`: `tart run --no-graphics --no-audio --no-clipboard --no-usb-accessories` with the
   export as the one read-only share; ready when `tart exec <vm> true` answers.
4. In the guest, through the guest agent: `keychain` (the login keychain is readable),
   `prepare-source` (the commit into `~/.ava/source`, a standalone clone with no remote),
   `toolchain` (the repository's own `scripts/provision/database.sh` and `toolchain.sh`:
   Homebrew `postgresql@17`, `redis@8.2`, `pgbouncer`, `pgvector`, and the pinned uv; a no-op
   for what the golden image already holds), `python` (`uv sync --locked`), the profile and
   observer uploads, `init` and `start` (the same argv as the container recipe; the first
   start builds the frontend), `observe`, `snapshot` (process table, disk, memory).
5. Evidence out and teardown, whatever happened: the observer's JSON and the cluster's logs
   are copied out, the VM is halted and deleted, the temporary export is removed.

Exit status is zero only when every step and every observer check passed and the VM is gone.
Rules the driver enforces before it asks Tart for anything: at most two Tart VMs run at once
(counting every running one), a clone is taken only from a stopped image, the share is
refused if it is, or contains, the home directory or the host's cluster, credential,
keychain or Tart directories, and only a VM whose name carries the run prefix is ever deleted,
so the golden image and earlier experiment VMs are out of its reach. The model is scripted
and nothing secret is passed in, as in the container.

`tart_golden.py` is the golden-image build, done once with a person present: clone the base
image by digest, set 4 CPUs and 8 GB, fetch a commit and provision the toolchain, create the
identity, set the key's partition list, build and load the helper, then boot with graphics,
print what to toggle and wait. After the person confirms it restarts the helper and judges the
grant from `ping`, the system TCC rows, the identity's designated requirement and a capture
that is not a uniform image (the record is written to the evidence directory); it then removes
`~/.ava/source` and shuts the guest down. It never overwrites a VM, never boots a third, and
`--dry-run` prints every step and touches nothing. The steps are those of "Golden image" and
"First grant" below.

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

### Numbers (known, measured against registry manifests and releases on 2026-10-01, and by the run in Experiment B)

- **Install Tart from the release archive, with its sha256 checked.** Download
  `tart.tar.gz` from the release page of `github.com/openai/tart` (22.9 MB for 2.40.1;
  `cirruslabs/tart` redirects to it), compare its sha256 with the digest the release
  publishes (`tart_<version>_checksums.txt`; `363e2701154a8155cbc1bb6d845430c9b42697d2a186bc49574471ca2877db46`
  for 2.40.1), extract `tart.app`, and put a one-line `exec` wrapper on `PATH`: the binary
  runs from inside its bundle, which is also what Homebrew's own wrapper does. The app is
  signed with a Developer ID and notarized (`codesign --verify --deep --strict` and
  `spctl -a -vv` accept it).
- **Why not Homebrew.** Homebrew 7 refuses a formula from a tap it was not told to trust
  until `brew trust` runs, and that edits Homebrew's trust configuration. The
  `openai/tools` tap also lagged the latest release (formula 2.38.0 while the release was
  2.40.1) and its formula depends on `softnet`, which default NAT networking does not
  need. The Tart documentation's quick start still names the older `cirruslabs/cli` tap.
- **Pin the image by digest, not by tag.** The experiment used
  `ghcr.io/cirruslabs/macos-tahoe-base@sha256:1b093499716409d29e8b5336844528e1cae375db97d2ad8e5aeff78cf0da201e`
  (what `latest` resolved to on 2026-10-01; 96 layers; guest macOS 26.6.2).
  `tart clone <image>@sha256:<digest> <name>` pulls and clones in one step.
- Images `ghcr.io/cirruslabs/macos-tahoe-{vanilla,base}`: layers total 24.0 GB and 27.3 GB
  compressed; logical disk 50 GB, sparse. **Measured disk use:** the pulled `base` image
  plus its first clone took about 35 GB of the volume (free space fell by 35 GiB). Setting
  up the golden image, taking three clones and booting each added about 4 GiB in total, so
  a clone costs on the order of 1 GiB. `du` reports about 31 GB for every VM directory
  because the shared APFS blocks are counted in each; `tart list` shows the logical size.
  The pull took about 75 minutes, limited by the network.
- `vanilla`: SIP on, auto-login, SSH, passwordless sudo, no guest agent. `base`: vanilla
  plus Homebrew, git, Node, the Command Line Tools (`swiftc` is present), the Tart guest
  agent (`tart exec`), **SIP disabled**, and pre-written TCC rows for ssh, osascript,
  python and the guest agent, including a Screen Recording grant for the guest agent
  ([templates](https://github.com/cirruslabs/macos-image-templates/blob/main/scripts/update-tcc-database.sh)).
  Credentials `admin` / `admin`. **The decision is `base`** (see Decisions).
- Licensing: at most two macOS VMs per Apple host, from the macOS license (section
  2B(iii), "up to two (2) additional copies", purposes include software development and
  testing) and enforced by the kernel
  ([Tart discussion](https://github.com/cirruslabs/tart/discussions/1054),
  [kernel quota](https://khronokernel.com/macos/2023/08/08/AS-VM.html)). Whether Linux
  guests count toward that quota is not established by these sources. **Two guests at 4 GB
  each ran at once** on a host with 24 GB of memory (known), so the ceiling is the license's
  two, not memory. Tart itself is
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
- **A headless guest has no display device.** `--no-graphics` removes it. The guest still
  has an auto-login session and a launchd-loaded helper, and both `ping` booleans
  (`preflight_screen`, `ax_trusted`) and the TCC rows are readable (known: the second boots
  of R1 and R2 were headless). But `screencapture` has no screen to read, so a failed
  capture in a headless guest cannot tell a missing grant from a missing display. Every
  check that needs a capture boots with graphics (a window on the host's display; `--vnc`
  was not tried).

### Golden image (one-time, user present)

From `macos-tahoe-base` pinned by digest, 4 CPUs and 8 GB (`tart set <name> --cpu 4 --memory 8192`).
Steps 1 to 4 prepare a signed, loaded helper; the experiment ran them by hand (known), and
`tart_golden.py` scripts them, with the toolchain for a full start added (step 1).

1. **Toolchain.** `swiftc` is already in the image. Add `uv` at the release CI pins,
   installed from the release archive with its sha256 checked, and a checkout of the commit
   at `~/.ava/source` (a checkout at that path anchors the home to `~/.ava`), then
   `uv sync --frozen`. A full start also needs `redis@8.2`, `pgbouncer`, `pgvector` and Node
   22 (design).
2. **Identity.** With the same code the start path uses,
   `services.permissions_helper.lifecycle.ensure_signing_cert()` imports the self-signed
   identity with `-T /usr/bin/codesign -A`.
3. **Key access: the one step the start path does not do.** On a headless guest the first
   `lifecycle.converge()` fails in the signing probe: signing with the new identity blocks on
   an interactive dialog nobody can answer, the probe times out after 20 seconds and
   converge raises `PermissionsHelperSigningUnavailableError`. The keychain is not locked;
   what blocks is the private key's partition list, which the import leaves short of
   `codesign`. Set it once, with the remedy `services/permissions_helper/lifecycle.py` names:

   ```
   security set-key-partition-list -S apple-tool:,apple: -s \
     -l 'Ava Permissions Helper Code Signing' -k <account password> <login keychain>
   ```

   The key's access control lives in the keychain file, so a clone signs without a dialog:
   each of the five clone boots of the experiment (three with graphics, two headless) passed
   a signing probe before any unlock.
4. **Build, sign, load.** `lifecycle.converge()` builds the helper with `swiftc`, signs it
   with the hardened runtime, pins the designated requirement to the identity's SHA-1,
   writes the LaunchAgent, bootstraps it and pings. At this point the helper answers with
   both booleans false.

No cluster is started in the golden image, so it carries no cluster state; the helper
artifact under `~/.ava/helper` and its LaunchAgent stay in it (route R1 below).

### First grant (user present, once)

1. Boot the golden image with graphics (`tart run`, no `--no-graphics`). At start the helper
   registers itself in both lists (`registerPermissions` in
   `services/permissions_helper/helper/main.swift`), so both entries already exist and need
   only a toggle.
2. System Settings > Privacy & Security > Screen & System Audio Recording: turn on
   AvaPermissionsHelper. Accessibility: turn on AvaPermissionsHelper. Authenticate with the
   account password.
3. **Restart the helper after the toggles.** Either choose "Quit & Reopen" when macOS offers
   it (never "Later"), or run `launchctl kickstart -k gui/<uid>/<label>` in the guest. macOS
   applies a Screen Recording grant to a process only if the process started after it: in
   the experiment the helper that was already running reported `preflight_screen` false for
   as long as it lived, while the system TCC row already said allowed and the `screencapture`
   child it spawns already worked. `ax_trusted` flipped to true in the same process about a
   minute after its first false reading. After the restart both were true.
4. Verify, in the golden image, before anything is cloned: `ping` reports both booleans
   true; the two rows for `com.ava.permissions-helper` in the system `TCC.db` have
   `auth_value` 2 and a code requirement that embeds the identity's SHA-1; a real capture
   through the helper is not a uniform image. Record the identity's SHA-1, the designated
   requirement (`codesign -d -r-`) and the rows.
5. Shut the guest down with `sudo /sbin/shutdown -h now` (plain `shutdown` is not on sudo's
   `PATH`) and wait for `tart run` to exit. `tart exec` reports "Transport became inactive"
   at that moment, which is normal. Clone only from a stopped image.

Grant routes: (1) click once in the golden image, then clone (taken; it works, see
Experiment B); (2) on the SIP-off `base`, write the rows into `TCC.db` with a code
requirement compiled from the helper's designated requirement (the Cirrus script does this
for other clients; Ava's own notes call direct writes unverified on macOS 26; the user
database moves into a per-user container in macOS 27); (3) MDM cannot grant Screen
Recording, only deny it or let a standard user approve
([PPPC overview](https://www.iru.com/blog/archive/changes-to-pppc-in-macos-big-sur)).
Routes (2) and (3) are not needed while route (1) holds.

### Per run

`tart clone` the golden image; `tart run --no-graphics --dir=src:<repo>:ro` (add graphics
only for a check that needs a capture); wait until `tart exec <vm> true` succeeds (5 to 15
seconds in the experiment). The LaunchAgent loads at login and the helper answers `ping`
with both booleans true about a minute after boot, while the guest is still busy starting.
The login keychain needs no unlock in the boots tried (auto-login opens it); keep
`security show-keychain-info` as a guard and unlock only if it fails. Clone the commit from
the shared directory into `~/.ava/source` (local disk, not the virtio-fs share), sync
dependencies, start, observe, copy evidence out, stop and delete the clone. Observation
runs in the guest; a host browser pointed at a forwarded guest port has the same
port-number problem as in Recipe A. The container's observer is the guest's: one file
(`observe.py`) that reads only the home and the checkout, with the Homebrew kegs for the
toolchain and one macOS-only check, `helper_chain`. `tart_run.py` is this section as a
script.

**A helper rebuild inside a used home needs the job retired first.** When the commit under
test changes the helper's inputs, the start rebuilds it, and replacing an installed, loaded
helper is refused by design (read in the code, not triggered in the run: `install_and_load`
raises "loaded helper differs from the requested artifact"; `require_retired_helper`
demands no loaded job, no plist and no live process). The order R2 followed, and that
worked: `launchctl bootout gui/<uid>/<label>` from outside the helper's tree, delete the
LaunchAgent plist, delete the home, re-clone, sync, then `lifecycle.converge()`. A build
that changes nothing is skipped, so most runs never reach this.

### Observing a guest

`observe.py` runs the container's six checks and, on macOS, `helper_chain` (on Linux it is
listed under `not_applicable`, neither passed nor failed). `helper_chain` reads and changes
nothing:

- **`ping`** through the real client: `preflight_screen` and `ax_trusted`, plus the two
  lifecycle protocol flags the start admits the helper by. Readable headless.
- **The tree**: the helper's root keeper reports a running root for this home, the root's
  own status agrees on its pid, and every unit's chain of parents ends
  `unit -> ava-root -> helper -> launchd`, with the units exactly the verification profile's
  services.
- **TCC rows**, read-only: `sqlite3 -readonly` on the system database
  (`/Library/Application Support/com.apple.TCC/TCC.db`), compared with the two allowed
  values; the helper's designated requirement and CDHash are recorded beside them. The
  Screen Recording and Accessibility rows for `com.ava.permissions-helper` live there, not
  in the user database. `auth_value` 2 is allowed, 0 is denied; the helper writes the denied
  rows itself at its first start, and its csreq embeds the identity's SHA-1. Never write
  these rows to make a check pass.

Beyond the observer, for a person or a boot with graphics:

- **A real capture through the helper** (graphics only), judged by pixel statistics (distinct
  colors and the share of the commonest one) and by looking at the image, copied out as
  base64 over `tart exec` output.
- **The guest agent as a second observer.** The `base` image pre-grants Screen Recording to
  the guest agent, so `tart exec <vm> screencapture -x <file>` shows the screen whatever the
  helper holds. That is how a dialog in an ungranted clone is seen without touching it.
- **What shows on screen after boot.** Every granted guest shows an "App Background
  Activity" banner for the LaunchAgent (the background item is `[enabled, allowed,
  notified]` in `sfltool dumpbtm`). It is informational, needs no click, and is not a
  consent dialog; a screenshot check for dialogs must tell it apart. An ungranted clone
  shows a real Accessibility consent dialog (Open System Settings / Deny); do not answer
  it, because answering writes the TCC row.

### Limits

Two macOS VMs at a time, golden image included while it runs (known: two guests ran at
once). The helper's grants are keyed to the bundle id and the certificate's designated
requirement. The onboarding note records that a same-identity rebuild kept the Screen
Recording and folder grants, an identity change drops them, and other notes say a rebuild
resets Accessibility and some extended rows. In R2 a same-identity, same-source rebuild
reproduced the same CDHash and kept both grants; it did not change the code, so whether a
rebuild that changes the CDHash resets Accessibility is untested (unknown 2). A rebuild is
skipped when the source and the requirement hash are unchanged.

### Run results

One run of `tart_run.py` against the golden image of Experiment B: the helper built, signed
and loaded, both grants given, uv present, and no PostgreSQL, Redis or PgBouncer. Known, from that run (macOS 26.6.2 guest on `arm64`, 4 CPUs and 8 GB, headless;
the project commits no run, so this is what the run established, not a log):

- **The run passed, and the VM was deleted.** All seven observer checks passed: the six
  of the container (`toolchain`, `source`, `services`, `frontend`, `cors`, `scripted_agent`;
  the scripted agent's recorded output body was `3`) and the macOS-only `helper_chain`. In
  that check the helper answered `ping` with both grants true, the system TCC rows for both
  services were `auth_value` 2, its designated requirement and CDHash were those the golden
  image's own verification records (same identity SHA-1, same CDHash), and every process chain ended `unit -> ava-root -> AvaPermissionsHelper -> launchd`
  for the gateway, frontend, agent-host and ops. The helper in those chains had been running
  since 14 seconds after the guest's boot (its process age against `launchd`'s in the
  snapshot): `ava start` ran its converge step (build, sign, load) and left it in place.
- **No manual step.** Nothing in the run waited for a click, an unlock or an answer. The
  guest was headless, so a consent dialog could not have been seen, but none blocked a step.
  This is the claim "a clone of a granted image carries `ava start` on macOS" holding end
  to end, beyond the helper's `ping` that Experiment B measured.
- **Time**, 21 minutes in all:

  | Step | Seconds |
  |---|---|
  | export, clone (APFS copy-on-write), boot to guest agent | 2, 0, 25 |
  | keychain guard, `prepare-source` | 1, 15 |
  | `toolchain` | 743 |
  | `uv sync --locked` | 4 |
  | uploads, `ava init` | 2 |
  | `ava start` (first start: data plane, frontend dependencies and build) | 472 |
  | `observe`, `snapshot`, evidence copy and teardown | 5, 1, a few |

- **The `toolchain` step is what makes a run slow, and it is a golden-image matter.** The
  4 seconds of `uv sync` say the image's uv cache was warm (inferred from the time). The
  image held none of the four Homebrew formulae, so each run installed `postgresql@17`
  (17.11), `redis@8.2` (8.2.9), `pgbouncer` (1.25.2) and `pgvector`, dominated by the
  PostgreSQL bottle download from ghcr.io at roughly 40 to 90 KB/s on the network the run
  used. `tart_golden.py` runs the same step when it builds an image, so a golden image built
  by it carries them and the step is a no-op per run; this run did not use such an image.
- **Versions the guest ran** (recorded by the observer): PostgreSQL 17.11, Redis 8.2.9,
  PgBouncer 1.25.2, uv 0.10.2, Python 3.12.12, Node v24.20.0 and Next.js 16.3.4. Node comes
  from the base image; `scripts/provision/node.sh` is not run on macOS, and the frontend
  built and answered on 24. The Linux image installs Node 22. Homebrew's PgBouncer is
  1.25.2, not the 1.26.0 the Linux image pins: on macOS `base/host/brew_pin.py` names the
  formulae and `brew install` takes the version Homebrew serves.
- **Memory.** The guest has 8 GB, and a macOS guest has no cgroup, so no peak is recorded;
  after the start the guest reported 81% free and no swap in use.
- **Benign output.** The start warns that the standalone checkout has no installed Git
  hooks (as in the container) and npm warns that three packages have install scripts not
  covered by `allowScripts`.
- **A killed run leaves an invisible half-made VM.** A run that is killed hard during
  `tart clone` (no `finally`) left a directory under the Tart VM store holding `config.json`,
  `control.sock` and `nvram.bin` and no disk image. `tart list` does not show it, and `tart
  delete <name>` removes it.

The host side ran under the Command Line Tools' Python 3.9, which the stdlib-only scripts
accept. Not exercised by the run: a commit that changes the helper's inputs (the start would
then need the retired-helper order above), a capture, a boot with graphics, and a golden image
that drifted over days.

#### The golden build, as exercised

Two parts of `tart_golden.py` ran for real; the part between them did not.

- **From the base image, as the script runs it.** `tart clone` of the pinned digest (APFS
  copy-on-write, instant), `tart set`, the headless boot with the commit export,
  `prepare-source` (4 to 8 seconds) and `toolchain` (407 seconds on a fresh image, uv included)
  ran. Of three attempts the first two failed within 40 seconds at the Homebrew bottle
  downloads (`SSL_ERROR_SYSCALL` to ghcr.io; `toolchain` is bounded and a failed step stops
  the build with the VM left as it is). The third passed `toolchain`, and then `python`
  (`uv sync --locked` on a cold cache) failed after 656 seconds on a request timeout to
  files.pythonhosted.org. A retry of the sync against a PyPI mirror from the same guest timed
  out as well. So on a fresh image the script has not reached the identity, the key partition
  list, the first helper build or the wait for the person; the network the attempts used
  reaches ghcr.io and PyPI only intermittently and slowly, and a cold-cache `uv sync` is the
  step that needs it most.
- **Against the granted image.** `prepare-source`, `python` (8 seconds, warm cache),
  `IDENTITY`, `KEY_ACCESS`, `HELPER`, `RESTART_HELPER`, `VERIFY` and `FINALIZE` ran in a
  clone of the granted image booted with graphics, and every one exited zero. Creating the
  identity, setting the partition list and converging the helper were no-ops there (the image
  already had them). `judge_grant` returned no problem on the real record: both booleans
  true, both system TCC rows `auth_value` 2 carrying the identity's requirement, and a capture
  through the helper of 275,424 distinct colors with the commonest at 6% of the pixels. This
  checks the verdict's input formats (`security find-identity`, `codesign -d`, the `sqlite3`
  rows) against real output, which the unit tests only mimic.
- **Not run by the script anywhere:** the interactive wait (`await_grant`), and creating the
  identity and the grants from nothing. They stay design plus unit tests until a golden image
  is built from the base image with a person present.

## Surfaces only macOS can verify, and what CI keeps

| Surface | Why macOS | Hosted macOS CI today | Tart VM |
|---|---|---|---|
| Helper signed with the stable identity | needs imported-identity codesign; hosted runners cannot (comment in `ci.yml`) | ad-hoc signature only | yes |
| launchd -> helper -> root -> services ancestry; keeper conflict and restart | launchd | yes (`python -m scripts.ci.two_section_chain_smoke --skip-attribution`) | yes, plus tccd attribution |
| Desktop grants and their attribution | tccd and the consent UI | no | yes |
| Computer capability (capture, click, type) | grants, GUI session | no | yes |
| Headed Chrome and browser profile | macOS Chrome, GUI session | no | yes |
| macOS data plane (vendored PG, brew Redis and PgBouncer, SysV shm limits) | mac binaries | Linux-only pgvector smoke | yes |
| `ava start` end to end on macOS | helper refuses ad-hoc, no direct-spawn fallback | not possible without a test hook | the only place besides a real Mac |
| LaunchAgent jobs (health probe, logs, packages, boot) | launchd | no | yes |

Nothing above moves to the hosted runner without weakening the no-ad-hoc, no-fallback rule
in the helper, so they stay on the VM.

## Not yet known

Tart. Items 1 to 3 are answered by Experiment B and stay here so the numbers used above
keep their meaning; the Recipe C run answers the question Experiment B left (`ava start` in a
guest) and adds items 6 to 9:

1. **Answered: a clone of a granted image keeps the signing identity and both grants.** The
   SHA-1, the designated requirement, the CDHash and the TCC rows were identical in the
   golden image and in every clone, both helper booleans were true on the first boot, and
   the ungranted control reported false. One clone per verification is enough; no click per
   clone.
2. **Answered in part: a helper rebuilt in a clone (fresh home, same identity, same source)
   keeps both grants** and reproduces the CDHash. Still untested: a rebuild that changes the
   CDHash (a source change), which is the case the client's own diagnostic says resets
   Accessibility once.
3. **Answered: the helper chain and signing work under `tart exec` and the auto-login
   session with no approval at clone time**, once the key's partition list is set in the
   golden image; the keychain needed no unlock at any of the five clone boots. Two things the
   design missed:
   the partition list (not a locked keychain) is what stops signing, and a Screen
   Recording grant reaches a helper only after it restarts (see "First grant").
4. Whether Screen Recording re-prompts on a schedule in a long-lived golden image (macOS 15
   prompts monthly per app; state lives in
   `~/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist`; macOS
   26 behavior not checked).
5. Whether Linux guests count toward the two-VM kernel quota, and whether the Linux
   container runtime's VM does. (Two macOS guests at once is known to work.)
6. **Answered: `ava start` runs end to end in a cloned, headless guest** and the observer's
   seven checks pass, with no manual step (Recipe C, "Run results").
7. A golden image built from the base image by `tart_golden.py`, to its end and with a person
   present: the build has run to the dependency sync and, against a granted image, from the
   helper steps on; the join between them and the grant wait have not run. A run on such an
   image would show whether the per-run `toolchain` step (743 seconds here) becomes a no-op.
8. A commit that changes the helper's inputs: the run only met an unchanged helper, and
   `tart_run.py` does not automate the retired-helper order above, so by the code read there
   the start would refuse such a commit (not triggered). Whether a CDHash-changing rebuild resets Accessibility is item 2.
9. How the golden image ages: Homebrew formulae, the uv cache and the base image's own
   updates move under a long-lived image (compare item 4).

Container:

10. Whether the impersonation preview can run in either boundary: it needs external host CLI
    logins, which would have to arrive as dedicated revocable credentials, and the scripted-
    model ruling means no secret enters a boundary today.
11. Whether the same recipe passes on `linux/amd64` (the architecture CI and most Linux hosts
    use): nothing in it is arm64-specific except the package install, but it has only been
    run on arm64.
12. A browser UI check (Chromium in the image) and real provider behavior are not carried;
    each needs its own decision.

## Experiment B: clone inheritance, outside the repository (run 2026-10-01)

Answered unknowns 1 to 3 and decided that the Tart recipe is clone-per-run, not
click-per-run. It ran in parallel with Recipe A. The protocol, as run:

1. Clone `macos-tahoe-base` (by digest) to `ava-golden`; 4 CPUs, 8 GB.
2. In the golden image: check out the commit at `~/.ava/source`, create the identity, set the
   key's partition list, build and load the helper, and (user present) grant Screen Recording
   and Accessibility by hand, then restart the helper ("Golden image" and "First grant").
   Record: identity SHA-1; `codesign -d -r-` output; TCC rows for
   `com.ava.permissions-helper` (user and system databases, including the code requirement
   blob); helper `ping` fields `preflight_screen` and `ax_trusted`; a real capture that is not
   uniform. Shut the golden image down.
3. A clone taken **before** the grants is the negative control (taken while the golden image
   was stopped, never booted before the grants). After the grants, two more clones: R1 keeps
   the helper artifact in its home. R2 is booted once, then its helper job is retired
   (`launchctl bootout`, plist deleted), `~/.ava` (source included) is deleted, the same
   commit is cloned again, and `lifecycle.converge()` rebuilds the helper.
4. Boot each one at a time, with graphics so a capture is possible, and repeat the recording.
   The login keychain was checked and unlocked first.
5. Stop and start R1 again headless; then run R1 and R2 at once, headless, at 4 GB each.
6. Pass: R1 and R2 report the same SHA-1 and requirement and both grants true with no
   consent dialog; the control reports false. R1 only: bake the helper artifact outside the
   home. Neither: try route 2, else count the clicks per clone (two toggles and a password).

### Results (known)

Every guest, control included, carried one identity and one designated requirement
(`identifier "com.ava.permissions-helper" and certificate leaf = H"<the identity's SHA-1>"`),
and one CDHash (read directly in the golden image, in R2 after its rebuild and in the
concurrent run; the other guests carry the byte-identical helper file).

| Guest | `preflight_screen` / `ax_trusted` | system TCC `auth_value` (Screen Recording, Accessibility) | Real capture | Consent dialog |
|---|---|---|---|---|
| golden, helper started before the grant | false / false, then false / true | 2, 2 | not uniform | none seen |
| golden, helper restarted | true / true | 2, 2 | not uniform | none |
| R1, first boot | true / true | 2, 2 | not uniform | none |
| R1, second boot (headless) | true / true | 2, 2 | no display | not observable |
| R2 before the rebuild | true / true | 2, 2 | not uniform | none |
| R2 after the rebuild | true / true | 2, 2 | not uniform | none |
| R2, second boot (headless) | true / true | 2, 2 | no display | not observable |
| control (clone taken before the grants) | false / false | 0, 0 | failed (`screencapture` exit 1, with graphics) | Accessibility consent dialog |

Two more boots ran at the same time (R1 and R2, headless, 4 GB each) and both reported
true / true with the rows above.

What the run showed:

- **The clone keeps identity and grants.** The SHA-1, the designated requirement, the CDHash
  and both TCC rows (auth value and code requirement blob) were identical in the golden
  image and in both clones. Helpers that started at clone boot reported both booleans true
  at their first `ping`, with no restart and no dialog seen. The result is the pass case:
  clone-per-run, and the R1 route (helper artifact left in the image) is enough.
- **The keychain file hash is not an invariant.** It differs after every unlock or boot
  (macOS rewrites the file), so compare the identity, the designated requirement and the
  code requirement in the TCC rows instead.
- **R2's rebuild kept the grants.** The rebuilt helper has the same CDHash and the same
  source hash as the original; its file sha256 differs only because the signing time is
  inside the Mach-O. Both booleans were true at once, with no dialog.
- **The negative control shows the prompt path works.** Without the grant the helper
  reported false / false, its capture failed even with a display, and macOS put up the
  Accessibility consent dialog. It also shows why a failed capture in a headless guest
  proves nothing: here, with a display, the failure is the missing grant.
- **Timing.** Guest agent ready in 5 to 15 seconds; helper answering `ping` with both
  booleans true about a minute after boot; `converge()` 39 seconds for the first build in
  the golden image (identity already present) and 5.5 seconds for R2's rebuild.

Not exercised by the experiment: a rebuild that changes the CDHash; `ava start` (services,
data plane, root) inside a guest (the Recipe C run covers it); a capture from a headless guest
with the grants held (no display); `--vnc`; the golden image drifting over days (unknown 4);
Linux guests against the two-VM quota.
