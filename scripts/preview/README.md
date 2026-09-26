# Local branch preview

`local.py` resolves a local branch, fetched remote ref, or commit to one immutable
commit before preparing a disposable cluster. It can exercise an unmerged branch
without waiting for CI or a production gateway. Its result is evidence for that
revision and profile; it does not replace CI or authorize production promotion.

```bash
python3 scripts/preview/local.py run --ref codex/my-change
python3 scripts/preview/local.py run --ref origin/main --keep
python3 scripts/preview/local.py check /absolute/path/to/run
python3 scripts/preview/local.py stop /absolute/path/to/run
```

`--repo /path/to/repo` selects another local repository (fetch remote branches
there first); `--root` selects another parent directory. Uncommitted working files
are not included, and a running preview does not follow later branch movement;
repeat `run --ref ...` for the next commit. The target revision must implement this
start entry, the core service roster and the scripted `message_flow` scenario; an
incompatible revision fails visibly. Git, uv, Node/npm and the native data-plane
prerequisites must be available. Run trusted branches only: home isolation is not
a sandbox for arbitrary source code.

The controller records the commit and requested ref under `~/.ava-previews/<run>`.
It prepares a detached worktree, the candidate's own Python environment and
frontend dependencies. It then invokes the candidate's normal `ava start` with
`--worktree`, a private configuration file, and a persisted allowlist of gateway,
frontend, ops and agent-host. Initialization, retry and stop belong to the normal
lifecycle; the controller has no alternate service launcher.

Gate is not in that allowlist, so open the printed frontend app URL directly: the
browser loads Next.js from the app port and calls the gateway cross-origin. The
last preparation step computes the port block that first start allocates from the
run's still-empty private registry and adds that exact loopback app origin to the
private configuration as the gateway's CORS allowlist.

Each run owns its home, registry, port reservation and native Postgres, Redis and
PgBouncer. It inherits only a small OS environment allowlist, with no production
URLs, bearer, provider keys, Python redirection or telemetry export. The model is
scripted, while agent creation, graph execution and `print(1 + 2)` run through the
actual gateway and agent-host. Success requires the recorded execution body to be
`3`, not merely a model response or a timestamp containing that digit. The
following observer check requires identity probes for all four services, a
frontend HTTP response and an authenticated CORS response for that exact browser
origin, so an origin that missed the allocated block fails verification.

On macOS the normal chain is `launchd -> signed helper -> ava-root`. Each run
uses its own helper artifact directory and home-specific job, preserving the
stable signing identity without replacing another home's artifact. Signing and
permission prerequisites must already be available. Linux runs ava-root without
a helper. This controller uses POSIX process and lock APIs.

A run normally stops in `finally`; `--keep` retains only a successful preview.
Teardown calls normal stop and destroy, then independently checks for surviving
processes, listeners and registry reservations. A failed cleanup stays failed in
`run.json`; the observer never deletes evidence to manufacture a clean result.
Logs and data remain for inspection. Concurrent lifecycle actions on one run are
rejected by the operation lock. Recorded foreground commands are reaped on
timeout or interruption; SIGKILL or power loss cannot run cleanup, so run `stop`
on the recorded directory when resuming. After confirmed cleanup, remove the
detached worktree with `git worktree remove --force <run>/source`, then the run
directory once its evidence is no longer needed.

This profile proves source startup and real agent execution. It does not prove
sealed release update/rollback, multi-machine coordination, real provider behavior,
browser/computer permissions, or production cutover. Those require their own
maintained scenarios using the same lifecycle and transition APIs.

## Linux lifecycle proof

Inside an isolated Linux machine with systemd, run the maintained scenario:

```bash
python3 -m scripts.preview.linux_cycle --ref codex/my-change
```

The candidate needs this scenario's observer module. The guest must already have
Python 3.12, uv, Git, Node/npm, PostgreSQL 17 with pgvector, Redis 8.2 and
PgBouncer available, plus noninteractive sudo for its private systemd unit.
For OrbStack, use an isolated Ubuntu machine with host sharing and private host
network access disabled; run this command inside the guest. The scenario does
not install OS packages or alter host/VM settings.

It checks first start, bare repeat, ordinary stop/resume, and the same home's
systemd start/stop/resume. Each successful start must immediately admit and
complete real agent execution. Independent native observations check PID1 as
root's parent, absence of a permissions helper, exact application/data process
births, listeners, service PATH, completed Redis diagnostics, stored agent rows,
and unchanged identity/configuration. systemd stop must close root/application
processes while retaining the exact data-plane births. Ordinary stop closes
both application and data processes; resume retains durable state.

Durable PTYs have their own lifetime. The scenario creates one idle terminal
through the existing terminal backend and requires its recorded host, shell,
generation and control identity to survive manager stop/resume. Other surviving
PTY workloads must also prove their native owner from the persisted record and
control socket; a process name is never sufficient. Full stop/destroy must close
these resources too. Observations retain process ancestry, command and working
directory so failed-run cleanup does not erase the classification evidence.

Every phase is recorded in `cycle-proof.json` and `cycle-<label>.json`. Cleanup
always uses ordinary stop/destroy and then independently verifies native and
registry absence. A failed phase stays failed even when cleanup succeeds; the
scenario never retries a failed admission probe or force-kills a survivor to
produce a passing result. Keep the run directory as evidence, and stop only the
named test VM after inspecting cleanup. This is lifecycle evidence, not release
upgrade/rollback or production approval.

An image-cycle controller can call the same observer with an explicitly
captured `cli.release_prepare` receipt:

```bash
python3 -m scripts.preview.linux_observer RUN LABEL manager-running \
  --runtime-receipt /absolute/path/to/preparation/receipt.json
```

The observer still executes from `RUN/source` with the private home and registry
environment. It verifies the receipt against that home's release store, the
current native platform, the complete image inventory, and the builder's source
identity. It never chooses an expected image from `current-release`. Omission
of the flag retains the source-runtime contract.

Both modes require exact root argv, executable, cwd, home/registry/virtualenv
environment and admitted PATH, alongside native birth, ancestry and listener
custody. Image argv must retain its isolation flags. Other environment values
are represented only by a digest; this does not independently attest arbitrary
configuration values or reveal credentials. Preparation evidence alone does
not establish startup readiness or a successful release transition.

The `validate.sh` and `spawn-samples.sh` scripts operate an explicitly selected
already-running preview home. They resolve that checkout's gateway and credentials;
they are not cluster initialization or release-promotion entrypoints.

## Scripted model in a sealed image

`release_fixture.build_fixture` packages the existing message-flow model and
scenario from the exact captured source archive into a separate proof-only wheel.
Only those files and their package markers are included. The offline build uses
explicit hash-pinned build-tool constraints, verifies output bytes against the
captured inputs, and retains its wheel/source receipt in exclusive work.

This wheel is an additional declared test input, not a production dependency
resolved by `uv.lock`. An image proof must install it through the ordinary sealed
dependency input and verify its import under the image's isolated interpreter
before startup. Adding a checkout to `PYTHONPATH` or copying the entire tests tree
would not prove a self-contained image. This packaging helper alone does not
establish image startup or a completed release transition.

For a completed-work Linux image A/B/A proof, first run the ordinary source
preview with `--keep` and prepare two complete images into that preview home's
release store with `cli.release_prepare`. Supply both captured receipts:

```bash
python3 -m scripts.preview.release_cycle /absolute/preview/run \
  --previous /absolute/preparation-a/receipt.json \
  --candidate /absolute/preparation-b/receipt.json
```

To acquire and prepare the image inputs through the maintained composition,
initialize the source preview first, then supply two explicit `Acquisition`
documents from `cli.release_prepare.acquisition_models`:

```bash
python3 -m scripts.preview.local run --ref FULL_SOURCE_COMMIT --keep
python3 -m scripts.preview.release_proof /absolute/preview/run \
  --previous /absolute/acquisition-a.json \
  --candidate /absolute/acquisition-b.json
```

Each document names its own exact committed source, approved uv and hash-pinned
build constraints, plus exact Node/npm inputs. Set its frontend `gateway_port`
to `RUN/config.json`'s `ports.gateway`; acquisition builds that public configuration.
Its `work` must be `RUN/release-proof/a/acquisition` or
`RUN/release-proof/b/acquisition`, respectively. The controller creates their
parents. Optional collector/plugin inputs retain the ordinary acquisition
contract. Neither document can substitute a moving ref or reuse failed work.
Both commits must implement this lifecycle and the same schema/scenario contract;
the cycle checks these before stopping source services.

The composition invokes each acquisition and preparation in a fresh source
interpreter. It verifies the original acquisition again after building the
scripted fixture and after preparing the image. Production wheels/requirements
are copied into a separate proof input directory; only the explicit hash-pinned
fixture wheel is added. `derivation.json` links both inputs and explicitly states
that the fixture was not resolved from the production lock. The production
acquisition receipt and original inventories remain unchanged. The final
`PreparationReceipt` bytes, digest and requested source commit remain bound
through the existing release cycle's effect preflight. The adapter reads each
receipt once and verifies that captured value before constructing image inputs;
replacement during the other image's preparation refuses before source stop.

The composition directly owns one finite Linux session per preparation. Nested
tool commands own separate groups inside that session. The outer owner retains
its unreaped leader, signals captured native tasks through pidfds, and requires
the whole live session to close before proceeding, even if the coordinator dies.
The shared stdlib-only libc adapter supplies pidfds independently of optional
Python build bindings; libc and kernel support are probed before any child starts.
SIGINT/SIGTERM cancellation is deferred while spawning and recording custody,
then delivered inside the closure guard. Python handlers retain pending signals;
deferral does not alter the signal masks inherited by executed tools.
`custody.json` records the original failure and whether closure was proved. An
unreadable native scan or failed closure remains unresolved and blocks the cycle.
This is not a sandbox for build code that deliberately creates another session;
forcibly killing the outer owner requires external reconciliation, not a retry.

`RUN/release-proof/proof.json`, per-image phase records, command logs, acquisition,
fixture and preparation receipts retain exact inputs, timing and failures. A
preparation failure leaves the initialized source preview running and preserves
partial artifacts; it never starts a transition or silently retries. Stop that
preview explicitly with `scripts.preview.local stop RUN` if it is no longer
needed. Once the release cycle begins, its finite-executor settlement and normal
cleanup remain the sole cleanup path; the composition cannot bypass unknown
custody. Failed composition work needs a fresh preview, not an in-place repair.
This Linux four-service path does not replace the separate cold/source-absence,
collector-delivery, plugin-behavior or schema-migration proofs.
Its first-start profile disables automatic builtin schedule provisioning, so
only the explicitly owned fixture agents enter the completed-work proof.

The controller verifies both full image inventories, native platform and source
identities. Before any stop, each image's isolated interpreter imports and
invokes the existing scripted `message_flow` fixture. Its installed fixture
bytes must match both images and the source HTTP smoke observer. The fixture
wheel is an explicit proof dependency; it is never injected through PYTHONPATH
or treated as a production model provider.

The scenario closes only its completed smoke agents through the normal graceful
final-terminate API, waits for durable termination and native terminal closure,
and retains identity/configuration/checkpoint digests. Source stop keeps the
private data plane alive. The initial selector CAS requires no existing image;
A starts through the same home's ordinary systemd boot unit and pinned boot
entry. Public `ava cluster update --prepared` performs A→B, then a separately
captured B→A request. Each transition must finish in the requested candidate
direction with a successful, closed finite executor; public resubmission then
retires that exact executor. A live, failed, recovered or retried operation
cannot count as a passing transition.

The first post-readiness action is real message/code-execution smoke, followed
by the explicit-image native observer. Ports, persisted home/configuration,
agent state and data-process births must remain identical; application births
must change. The exact prior application tree is captured after fixture closure;
every captured native birth must be closed before new business smoke. Unknown
native identity is a failure, even if later cleanup succeeds. This proves
completed-work same-schema replacement and return,
not in-flight recovery, schema migration, fleet rollout or provider behavior.

`release-cycle-proof.json` records phase timings and receipt paths;
`release-inputs.json` records captured image identities and request hashes.
`release-*-completion.json`, `release-*-retired.json`, `release-frozen-*.json`,
`release-state-*.json`, `release-apps-*.json`, `smoke-release-*.json` and
`cycle-release-*.json` retain
native, journal, workload and independent observer evidence. Every command has
its own timed run log. A failure remains failed even if ordinary cleanup passes.
Cleanup refuses destruction while an attempted executor has live or unknown
custody, never retries a transition, and uses normal stop/destroy plus independent
absence checks. Existing cycle evidence is never overwritten by another run.
