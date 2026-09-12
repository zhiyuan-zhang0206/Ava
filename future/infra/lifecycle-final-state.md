# Process / service lifecycle final state — design record (task #3195)

> Status: **decided (2026-09-12)** — user rulings G1–G6 + the fallback are in, the design
> surface is closed, and the implementation phase has started (task #3195, slices P1–P7).
> Migration is a one-shot cutover; the window is booked with the user directly, and the
> user watches the cutover live. Decision record:
> [`decisions/2026-09-12-process-lifecycle-final-state.md`](../../decisions/2026-09-12-process-lifecycle-final-state.md).

| | |
|---|---|
| Author | CTO #3230 (writing); source material: #6124 (A/C inventory), #1818 (ops facts) |
| Date | 2026-09-12 · status = **final draft (draft-2)** — user rulings recorded (17:41): G1–G6 + fallback all decided, design surface closed, implementation phase started |
| Inputs | User order 2026-09-12 16:11 + the 16:08 three-step skeleton; the big-bang precedent (2026-08-07, applies to all root-design work) |
| Discipline | design first, implementation second; this phase touches no production and writes no code |

---

## Decision card (one page · rulings recorded)

| # | Decision point | Conclusion (user rulings 2026-09-12 17:40–17:41) |
|---|---|---|
| G1 | root shape | Decided: **two-section form**. macOS = launchd → permission-helper → **the helper pulls up root** (two independent programs/codebases); Linux/WSL/Windows = the system service manager pulls up root directly (the helper is not involved). Hard constraint: **zero permission content in root code** (isolation at the code layer). |
| G2 | supervisor boundary | Decided: **full tree** (including PTY / session hosts) |
| G3 | tree granularity | Decided: one tree per **(machine × $AVA_HOME)** |
| G4 | migration mode | Decided: **one-shot** (big-bang precedent) |
| G5 | naming | Decided: root = `ava-root`; the helper keeps its name |
| G6 | independent service group (gate/LGTM/redis-bridge) | Decided: **all go into the tree** |
| Fallback strategy | lose attribution vs lose service | Decided: **lose attribution, not service** (user, 17:41; two-section crash semantics in B5) |

> The decision surface ends here; everything below is detail and rationale, to drill into as needed.

---

## Summary

- **The problem**: OS-specific mechanisms are coupled into general runtime semantics — today's state mixes macOS-proprietary machinery (TCC/helper) with cross-platform mechanisms on one axis (dual spawn backends, the `AVA_PERMISSIONS_HELPER_SPAWN` switch, the half-migrated "pick a backend once per process" state). User, 16:08: "what I want is not to solve this problem, but that our architecture seems badly coupled to operating-system-specific things."
- **Final state in one sentence**: one **process tree** per (machine × $AVA_HOME) — a general root supervisor owns the ancestor slot and lifecycle authority for every long-lived process; the **OS edge answers only one question: "who pulls up root"** (macOS = the permission-helper **pulls up** root; Linux/WSL = systemd; Windows = the service manager). Core metaphor = tree; core invariant = **attribution tree = process tree (the chain is never truncated)**.
- **Key rulings**: see the **decision card** above (G1–G6 + fallback all decided); this draft implements the two-section form in full (zero permission content in root code = hard constraint).
- **As-is in one sentence** (#6124): OS-specific mechanisms are spread across 3 layers, 9 classes of OS-scheduled items, and 4 process-launch paths; on macOS, launchd simultaneously plays three roles (permission-attribution root / service keepalive / scheduled dispatch).
- **Rulings path**: draft-1 → #1818 fact verification → #405 → user rulings (17:41, G1–G6 + fallback all decided) → implementation phase started.

---

## 0 · Design concept (one page)

**What it does**:
- One **general root supervisor** as the **sole ancestor and lifecycle owner** of every long-lived Ava process on each (machine × cluster) — services, the agent host, and everything under them — with one set of lifecycle semantics across platforms.
- The **OS edge** collapses to one question: "who pulls up root". macOS = permissions-helper (using its stable identity to seed TCC attribution); Linux = systemd; Windows = the service manager; other systems = root bootstraps itself.
- One invariant: **attribution tree = process tree** — every Ava process's ancestor chain is complete, never truncated; under root there is no reparenting and no daemonizing.

**Why (the motivating case)**: today, macOS-proprietary mechanisms (TCC/helper) are coupled into general runtime semantics (e.g. dual spawn backends, the `AVA_PERMISSIONS_HELPER_SPAWN` switch) — the goal is **the architectural decoupling itself**; TCC attribution is just one concrete need of the macOS edge (user, 16:08: "what I want is not to solve this problem, but that our architecture seems badly coupled to operating-system-specific things").

**What it does not do**:
- No incremental / half-migrated states: no "pick a backend per process" dual track (helper/posixproc), no OS switches baked into general semantics.
- No OS-proprietary nouns in the general core (TCC/launchd/XPC/systemd do not appear in supervisor contracts).
- The supervisor does not carry application-logic scheduling (task orchestration / conversation flow is not its job — process lifecycle only).

**Core metaphor**:
- **Tree**: one process tree per (machine × cluster); root = the trunk (unique, stable, never migrates); units = branches; agents/sessions = leaves. The OS edge = whoever **plants the tree in the ground** (launchd/systemd/SCM).
- "Stable identity" is a property of the tree: the root does not move; the whole tree shares the root's OS identity (TCC authorization, signing credentials).

**Invariants (draft v0.2)**:
- I1 single root: exactly one tree per (machine × cluster); root has exactly one instance.
- I2 chain integrity: no process in the tree may reparent (no double-fork, no daemonize, no parent exiting); upgrades replace via exec, not exit+restart. **Its motivation is lifecycle management, reaping and patrol — attribution does not depend on it** (F2, 2026-09-12: attribution anchors statically at the spawn root, robust to every topology change).
- I3 attribution inheritance: the OS-level identity (macOS TCC) is inherited from root along the chain; restoring attribution = reseeding through root (rolling-restart the subchain that needs attribution).
- I4 OS-edge isolation: the general contract has zero OS-proprietary concepts; platform differences = a **closed enumeration**: (1) the mechanism that pulls up root, (2) process/signal semantics (including tree-termination semantics), (3) directory and path layout, (4) IPC form (unix socket / named pipe / TCP), (5) file-permission/umask semantics, (6) macOS: the TCC attribution carrier. Anything outside the enumeration must not enter the general core.
- I5 lifecycle completeness: the four verbs start/stop/restart/upgrade are fully defined on the tree and all preserve I2.
- I6 self-proving: every key property of the tree (I1/I2 state, restart counts, broken-chain events) is **observable and self-checkable at runtime** (see B7).

---

## A · OS-coupling as-is inventory (condensed; the full table with file:line evidence lives in the source material)

### A0 · The current three-layer structure
1. **Session/process execution layer**: `shared/session_backend.py` unified protocol + three implementations (posixproc / winproc / helperproc) + the `shared/_reparent` double-fork primitive.
2. **OS job/host layer**: four job writers (`os_cron` health probe / `os_autostart` autostart / `os_logs_job` logs / `os_watchdog_probe` probe) + the platform-capability ABC (`platform_backend.py`) + the low-level fact source (`platform.py`).
3. **Permission adaptation layer**: `services/permissions_helper/` (macOS Swift + Windows C#) + the status channels (`accessibility` / `screen_capture`) + firewall (`macos_firewall`).

### A1 · Ten key classes (condensed)

| # | Class | Representative mechanism | Final-state relation (recommended) |
|---|---|---|---|
| 1 | spawn backend & routing | dual switch gate + `get_backend()` choosing once per process; the helperproc/posixproc dual track | **Retired** (root is the single executor; the three implementations are restated as "root's platform execution implementations") |
| 2 | launchd jobs | 9 classes of scheduled items (autostart/health/watchdog-probe/logs/gate/LGTM/redis-bridge/helper + variants) — carriers differ across machines: health-probe is disabled on macmini (runs via a wsl cron), gate is not loaded on macmini (wsl = systemd), LGTM runs on wsl, redis-bridge is not running (a pre-migration leftover); plus 3 small jobs (caffeinate-display / bwh-sub-server / warn-error-audit) | The lifecycle group is **absorbed**; the permission group stays an **adapter (seeder)**; the independent group all goes into the tree (G6) |
| 3 | permission adaptation | the whole helper stack (signing / DR / path / protocol / nursery) + status channels | **Absorbed as the root** (the helper is a nine-tenths root form) |
| 4 | platform branching | the `platform.py` fact source + `sys.platform` hot spots | Converged to the OS edge (closed enumeration I4) |
| 5 | PTY/process hosting | per-session detached host (sovereign) + `_reparent` | **Full-tree ruling** (E2: semantic exemptions replace structural isolation) |
| 6 | runner mode | `AVA_RUNNER_MODE` **retired** (tests assert it); agent-host = the only execution architecture | Retired (historical concepts do not enter the final state) |
| 7 | remote topology | the machine/role composite model + cluster_rpc; no remote process handles | **Kept** (cross-machine = RPC to the target machine's root) |
| 8 | upgrade/restart | `ava start` three-way readiness, the stop drain boundary, update orchestration, helper self_upgrade execve | **Absorbed** (the root update protocol) |
| 9 | multi-cluster isolation | the per-cluster label/port/socket naming contract | **Kept** (tree granularity = machine × home) |
| 10 | win/WSL | the schtasks carrier, the winproc session model, the WSL dual track | Kept at the OS edge (Windows = the equivalence touchstone) |

### A2 · Cross-class findings (the five from #6124, key points)

1. **macOS's root candidate already exists**: permissions-helper is already nine-tenths of a "root service" (launchd direct start, stable signature, KeepAlive, **in-place self_upgrade execve**, process protocol, session table); the final state = the absorb-and-reshape path of "keep the seeder + add a separate ava-root" (the two-section ruling). Remaining gaps = (1) ownership of the persistent session registry (already decoupled to disk), (2) parent/child relationships with the posixproc/PTY process families, (3) chain continuity across helper self-updates. (Note: mechanism in place ≠ traffic switched — the spawn canary flag was unset on all three machines as of the 2026-09-12 13:31 check; "nine-tenths form" refers to mechanism items; do not misread it as already enabled.)
2. **"Attribution tree = process tree" collides head-on with three existing invariants** (case-by-case rulings in E2): (a) PTY host sovereignty (no teardown can reach it); (b) `_reparent`'s double fork (a historical zombie-prevention design); (c) the service_respawn lesson (decoupling → updates cannot kill it).
3. **OS-edge jobs split into three groups**: (1) pure Ava lifecycle (autostart/health/watchdog-probe/logs), (2) independent services (gate/LGTM/redis-bridge), (3) permission attribution (helper) — (2) is the biggest gray zone (gate's "an update must not black out the entry" is a deliberate property).
4. **Cross-machine semantics are already "no remote process handles"**: spawn = a DB row + wake + the target machine picks it up — a real basis for "root manages all local processes".
5. **Windows is the equivalence touchstone**: no launchd/TCC; the helper's "permission" = session identity, with Job Object / private console replacing process groups — if the concepts cannot land isomorphically, we are over-bound to macOS.

### A3 · Evidence index
- Full A table (34.5KB, per file:line): `~/.ava/workspaces/6124/3195-A-os-coupling-inventory.md`
- C concept material: `~/.ava/workspaces/6124/3195-C-concept-materials.md`
- Ops facts (full launchd set / process samples / win·wsl): `~/.ava/workspaces/1818/3195-facts-for-cto.md`
- Prehistory: `workspaces/5811/tcc-helper-design.md` (the TCC fully-helper design) + `workspaces/5805/tcc-audit-report.md`

---

## B · Final-state architecture (substance)

## B0 · Final state in one sentence
Each (machine × cluster) has one complete **process tree**: a general root supervisor holds the ancestor slot and lifecycle authority of every long-lived process (services / agent host / session hosts); OS differences answer exactly one question — **who pulls up root** (macOS = permissions-helper seeds attribution; Linux = systemd; Windows = the service manager; others = self-bootstrap).

## B1 · Components and responsibilities
**1) root supervisor (`ava-root`; the general core, one set of semantics across platforms)**
- Holds: tree ownership (the unit registry), lifecycle execution (start/stop/restart/upgrade), control-plane IPC, self-upgrade (exec replacement).
- **Hard constraint: zero permission content** (isolation at the code layer; nothing macOS-specific ever enters root code).
- Does not do: application-logic scheduling; it does not know why TCC/launchd/XPC/systemd exist.
- Shape: one resident process; at the tree's root position; its parent = the OS-edge artifact (macOS) or the system service manager (Linux/Win).

**2) OS-edge adapter (thin, per-platform)**
- Exactly three duties: (1) pull up root; (2) keep root alive (restarts by the system mechanism); (3) (macOS only) carry stable identity (the TCC attribution seed) + spawn root.
- macOS: `permission-helper` acts as the **seeder** (launchd starts it directly; it **pulls up and keeps alive root**; its permission-execution points stay) — it does not double as root.
- Linux: a systemd unit (user or system, per environment) pulls up root directly.
- Windows: a service-manager (SCM) service is the formal form (schtasks is the fallback).
- Boundary discipline: the adapter **does not manage units** and holds no lifecycle logic — it is only responsible for getting "the tree planted".

**3) unit (a tree node)**
- Types (closed enumeration): service (gateway/page_server/watchdog/agent_host/agent_ops/mcp_daemons…), session host (PTY host, spawned inside the tree by its owning service), turn child processes (agent.exec_child, turn lifetime, parent = agent_host).
- Every unit has a declarative manifest; the tree's shape = the direct result of manifests (no implicit members).

## B2 · Contracts (three, all stable across platforms)
**K1 control-plane IPC** (unix socket / named pipe; the form belongs to the I4 enumeration): `up(name)` `down(name)` `restart(name)` `status()` (returns a tree snapshot + health) `upgrade()`. The CLI (ava start/stop/…) and every management plane is a client of this protocol; **no process may manage units by bypassing root**.
**K2 unit manifest** (minimal, closed fields): `id` / `exec` (with argv) / `restart` (always|on-failure|never) / `attach` (parent unit id, default root). — Deliberately minimal; a new field must pass design review first (guard against field drift).
**K3 adapter contract**: `spawn_root()` / `keepalive semantics` (system level) / `identity carrier` (macOS) / `stop_root()`. Adapter implementations depend on platform mechanisms only.
**B2a testable constraint (the user hard constraint made concrete)**: zero permission content in root code = a **file/symbol-level lint rule** (CI-checkable) — the root codebase must not reference or mention permission-domain files and symbols (the helper protocol, TCC/AX/screen APIs, …); the rule and its exception list are checked in-repo, and violations block merge.

## B3 · Invariants (semantic expansion)
- **I1 single root**: one root instance per (machine × cluster); the tree root is unique. (Violation check = a startup mutex + patrol.)
- **I2 chain integrity**: no process in the tree may reparent (no double-fork/setsid/daemonize; a parent never exits because of a management action); upgrades = exec or subchain replacement. **As-is contrast (#1818 verification)**: **every chain root is detached** (a uniform fact) — existing services' parent chains end at init/the OS domain and are not constrained by any Ava tree (sampling window: macmini/wsl 16:1x–16:32); the two shapes = attached directly to init / wrapped by a PTY host (e.g. wsl `schedule_runner` = pty host→bash→runner; a shape distinction, not a platform difference) — I2 is exactly what the final state must reverse.
- **I3 attribution inheritance**: on macOS, the whole tree's TCC attribution = root's seeder (the helper); "reseeding" = restarting that subchain through root; no promise to retroactively re-attach existing processes (the OS does not support it).
- **I4 edge isolation**: the general layer has zero OS-proprietary concepts; platform differences = the closed six-item enumeration (see outline v0.2).
- **I5 lifecycle completeness**: the four verbs are fully defined on the tree.
- **I6 self-proving**: see B7.

## B4 · Lifecycle
- **start**: the adapter starts root → root reads manifests → pulls up units (order = relaxed: per-unit self-healing takes priority, see E3) → steady state (patrol + restart-policy enforcement).
- **stop**: the whole tree stops in reverse; a single unit = its subtree stops.
- **restart (unexpected/manual)**: root performs a "subchain replacement" on the unit — start the new instance first, then stop the old one (or per the restart policy); **root itself does not move during this, and the chain never breaks**.
- **upgrade (the core scenario)**:
  - **root does not restart for upgrades** (macOS: the helper never exits for upgrades — it is the attribution seed).
  - **supervisor self-upgrade = exec replacement** (same pid, same attribution; already proven by the self_upgrade pattern).
  - **unit upgrades = rolling replacement** (stop→start the subtree; the parent = root is always present): no single-point replacement truncates the tree.
  - (Pending F measurement: attribution behavior of an exec mid-chain vs an exec at the root position.)
- **Crash self-healing**: a unit crash = restart per its restart policy (the parent restarts the child; the chain does not move); a root crash = the adapter keepalive path (B5).

## B5 · Availability strategy (the core trade-off: lose attribution vs lose service)
**Tone: lose attribution, not service** (#405's leaning given to the user, as option A).
- Semantics: on a root/helper crash — (1) existing units **keep running** (today's reality is already detached independent processes; this semantics is compatible with the status quo) (2) new spawn/management actions pause (degraded state) (3) the adapter keepalive restarts root → root takes over.
- **Recovery semantics (adoption vs reseeding)**: after root recovers, existing units have two options —
  - **Adoption** = the management plane takes over (recovering lifecycle management for that unit only); **note: POSIX cannot restore a parent chain for existing processes — adoption does not re-attach the chain**. Attribution is a separate matter (F1/F2, 2026-09-12): no reparent-family trigger resets attribution while the chain root (helper) stays alive (16 scenarios enumerated; the anchor is static at spawn), so the actual loss case narrows to the chain root's own lifecycle events (helper death/restart; F12 to measure).
  - **Reseeding** = restart that subchain through root (a fresh parent chain) — **the only path that returns attribution-requiring subtrees to I3**.
- **Where I2 holds**: I2 (chain integrity) holds only in **steady state** (root present); a subtree adopted after a root crash is **an exception state inside the design** (written out explicitly, never silently assumed). Exception-state exit = reseed (attribution-requiring classes) or keep adopting until a natural restart (pure background classes).
- **Executable policy for the attribution-less state** (#405 requirement):
  - a) No permission lockdown of any kind (the security model = entry-point protection, peers inside the tree — aligned with the 2026-08-10 user ruling);
  - b) macOS sensitive operations are decided by the OS in the attribution-less state (popups are expected) — the policy = **minimize the attribution-less window** (fast reseed + exposed metrics);
  - c) automatic reseeding = controlled rolling (no thundering herd; per unit priority).
- **Helper crash/restart semantics (two-section specific; #405's re-check point)**: (1) the root tree **survives** (service does not stop); (2) the chain breaks (existing processes cannot be re-attached — POSIX); attribution loss is bounded to the chain root's own lifecycle events (F1/F2: no reparent-family trigger resets attribution while the chain root is alive; the helper-death case is measured by F12); (3) recovery = **reseeding**: after the helper comes back via the launchd keepalive → check the root chain's state → if broken: a controlled rolling rebuild of the root tree (per unit priority, attribution-requiring classes first); (4) **single-instance handling**: dispose of the old root orphan before reseeding — two modes routed per unit ("adopt" = keep service / "pull up fresh" = keep attribution); (5) the helper does not depend on root (zero permission content in root) — the two failure domains are isolated in both directions.
- **Counter-case (lose service, keep attribution) and its rebuttal**: the counter-case argues root going down = kill and restart the whole tree (keeping attribution consistent). Rebuttal: (1) attribution consistency is not a security boundary (the 2026-08-10 ruling); (2) killing the whole tree = a hard service interruption, and it does not recover attribution any faster (the restart takes the same time); (3) against the status quo (detached independent survival) it is a UX regression. → The verdict between the two = **option A** (the user ruled at 17:41: lose attribution, not service).
- Addition: the adapter keepalive semantics (launchd KeepAlive) are already provided by the status quo; the root-crash recovery target = seconds.

## B6 · Multi-cluster
- Tree granularity = (machine × cluster): multiple clusters on one machine = multiple trees (each with its own root/adapter registration; per-cluster slugs already work like this).
- Cross-machine topology: one tree per machine; cross-machine addressing = the gateway; "pulling up a unit remotely" = a control-plane request to the target machine's root (no bare cross-machine spawn).

## B7 · Observability and self-checking (proving I2 at runtime; preventing silent chain breaks)
- **Chain patrol**: root periodically (suggested 60s) walks its own tree — checking each unit's liveness and parent/child relationships; on macOS it also samples the responsible attribution (observable surfaces = tccd logs / ps chains). A broken chain = an event + a count.
- **Metrics (minimal set)**: broken-chain event count / unit restart counts / attribution coverage (the fraction of attribution-requiring units with attribution present) / reseed latency (root recovery → key units' attribution restored).
- **Status surface**: `status()` output = a tree snapshot (structure + health + metrics), consistent for CLI/monitoring/user.
- Design discipline: **tree properties are not assumed at design time; they are continuously self-proven at runtime**.

---

## E · Design it twice (two cases per key structural point → convergence recommendation)

## E1 · root identity (macOS): the two-section form (user ruling 17:40)

**Ruled form**: macOS: launchd → permission-helper (started directly) → **the helper pulls up root (ava-root)** → the whole tree; helper and root = **two independent programs/codebases**. Linux/WSL/Windows: the system service manager pulls up root directly; the helper does not participate.
**Hard constraint**: root code **must not contain any permission-related content** (code-layer isolation; the checkable mechanism in B2a); the phrasing "the helper is the root" is abolished.

### E1 record · evolution and design implications
1. The design group's original recommendation was fusion (helper-as-root); **the user ruled the two-section form + the code-isolation hard constraint** — the problem is upgraded to "designing two independent code/keepalive chains": root stays OS-pure (the general core); the only macOS-specific item on the two-section chain = the helper (seeder).
2. Key design points:
   a. **Keepalive chain**: launchd (KeepAlive) → helper; **the helper pulls up and keeps alive root** (a root crash = the helper restarts root) — the adapter duty (K3).
   b. **Crash semantics (B5)**: a helper crash → the root tree survives but the chain breaks; the attribution-loss case narrows to the chain root's own lifecycle events (F1/F2 correction, F12 to measure); recovery = controlled reseeding (a rolling rebuild of the root tree, see B5).
   c. **Single instance**: root is single-instance (I1); the helper's keepalive must handle the conflicting semantics of "an old root orphan still alive vs pulling up a new root" (B5).
3. Withdrawn: "the general core rides inside a macOS-proprietary process" — the problem disappears (root is an independent program); the original E1 supplement (arguments around the fusion case) is archived with the ruling and no longer listed as an option.

## E2 · supervisor boundary: tree membership + rulings on the three load-bearing conflicts

**Two cases**:
- **Case 1 "full tree"**: all long-lived processes (22 service sessions + agent host + **PTY hosts** + watcher sessions) and exec children enter the root tree (full parent chain); lifecycle actions are semanticized per unit type.
- **Case 2 "runtime only"**: only services + agent host enter the tree; PTY/exec keep today's independence (detached) — i.e. I2 stays incomplete, accepting that the chain has break points.

**Rulings on the three existing loads (#6124 finding 2)**:
- (a) **PTY host "sovereignty"**: its original purpose = sessions must not die when the agent exits/updates. **Convergence**: enter the tree + make it a **semantic property** — the unit type carries a "survives updates (survive-update)" marker; "structurally unreachable" is replaced by "semantic + observable" (reachable inside the tree, but the update protocol explicitly exempts it). Operational protections of the "no teardown can reach it" kind are abolished (that was the price of decoupling and conflicts with I2).
- (b) **`_reparent` double fork**: the mechanistic root of attribution reset (historically used for zombie prevention/decoupling, producing PPID=1 — **not on the tree's structured teardown surface; needs an explicit SIGKILL to reap**; demonstrated by `session-ttl-reap-orphan-child-survives`). **Retired** — forbidden by I2; "zombie prevention" moves to root as the reaper (reaping exited children is the tree root's duty) + the single-session blast radius is preserved via "root's SIGKILL semantics toward a unit" (SIGKILL of one unit does not harm the tree: root pulls that unit back up).
- (c) **The service_respawn lesson (decoupling → updates cannot kill it)**: **replaced by semantics** — a unit inside the tree is naturally killable (root owns the kill); "protect certain units from being killed by a mistaken update" goes through explicit exemption markers (same as (a)), no longer through structural decoupling.

**Assessment**: case 1 is conceptually complete (I2 has no exceptions), and all three conflicts have explicit rulings; case 2 keeps the current structure but I2 has visible break points (PTY/exec are naturally outside the tree), and "who owns these processes" is conceptually dangling. **Convergence recommendation: case 1, the full tree** — the decoupling goal demands a complete chain concept; all "protective isolation" becomes "semantic markers + observability", paired with B7's self-proof. (The G2 decision point, including the action-granularity matrix: unit type × lifecycle action)

## E3 · Dependencies and self-healing: supervision topology and startup order

**As-is** (#6124 C-7): supervision chain = OS scheduler → watchdog probe (60s) → watchdog daemon (60s) → healthchecks → service sessions (4–5 layers of recursive fallback).

**Two cases**:
- **Case 1 "declarative dependency order"**: root pulls up units in DAG topological order (explicit depends_on), scheduled startup.
- **Case 2 "self-healing first + gating"**: root pulls up in parallel (no scheduling order); dependencies act only as **startup gates** (inheriting today's gate/requires_db gate form); each unit "self-heals" — health probe + restart policy (absorbing today's respawn_and_verify semantics: a restart counts as successful only once the probe confirms it); **the supervision chain converges from 4–5 layers to 2**: the OS adapter keeps root alive (KeepAlive/restart=always) + root keeps the whole tree alive.

**Assessment**: case 1 introduces a DAG concept and scheduling state (field-drift risk; conflicts with K2's minimal manifest); case 2 reuses today's gate + probe semantics, supervision layers **5→2** (the biggest simplification), and keeps "recursion terminates at the OS" (root's keepalive is the adapter's duty). Keep It Simple awards it to case 2. **Convergence recommendation: case 2** — startup = parallel pull-up + gating; steady state = healthchecks/restarts built into root; the OS probe and the user-space watchdog daemon **are both absorbed by root** (their duties fold into root; only the OS adapter's single layer of root keepalive remains).
(Preserved semantics: start's "three-way readiness" and stop's "drain boundary" are inherited as-is — C-5.)

## E4 · Minor structural points (selected)
- **IPC form**: unix socket (POSIX) / named pipe (Windows) — extending the helper wire-protocol genre (single-line JSON) into the control plane; the form belongs to the I4 closed enumeration.
- **Log ownership**: unit stdout/stderr is collected centrally by root into `$AVA_HOME/logs/<unit>/` (inheriting today's logs-maintenance retention policy; the OS job layer's logs maintenance is absorbed).
- **Upgrade-window semantics**: inherit today's (allocation freeze + generation boundary + the gate's independent entry); gate placement in D (the independent service group ruling).

## E · Open items (tracked with review)
- [ ] gate/LGTM/redis-bridge (the non-session service group) placement ruling → G6
- [ ] E2 "action-granularity matrix" details (unit type × lifecycle action) → draft-2
- [ ] Empirical backing for the E2(a)(b) replacements → the F list (F6/F7)

## C · Full mapping of current concepts (as-is → final state)

| As-is concept | Final-state mapping | Notes |
|---|---|---|
| units / clusters ($AVA_HOME = cluster; unit = a machine_units row) | **Kept vocabulary** | In the final state "unit" = a node of the root tree; machine = a composite read model (kept). Cluster = the boundary of one tree |
| service sessions (22 core roster sessions @91c6b4735; the roster = the full set, each machine runs a subset per capabilities — observed active: wsl 18 / macmini 9) | **unit (the main body)** | The roster IS the unit list; supervision passes from the watchdog chain to root |
| PTY sessions (sovereign hosts) | **unit subclass** (survive-update marker) | See E2(a): enter the tree + semantic exemption; "structurally unreachable" is retired |
| agent host (the only agent execution architecture) | **unit (type: agent-host)** | wake→turn semantics unchanged; pulled up and kept alive by root |
| ava start / stop / update | **clients of root's control plane** | start's "three-way readiness", stop's "drain boundary", update's orchestration semantics are inherited as-is; the implementation = IPC to root |
| remote topology (DB row + wake; no remote handles) | **Kept** | Cross-machine operations = RPC to the target machine's root (the cluster_rpc channel is inherited); process ownership = the target machine's tree |
| nursery (the helper's in-memory child-process table) / watchdog (three layers) | nursery semantics **fold into root**; watchdog **absorbed across two layers** | root = whole-tree reaper + nursery; the user-space watchdog daemon and the OS watchdog-probe merge into "healthchecks built into root"; only the adapter's keepalive of root remains (supervision chain 5→2, see E3) |

## D · Relation to the current helper-spawn canary

**Absorbed** (the final state picks up directly):
- The helper keeps its **permission-helper (seeder)** role — launchd direct start, pinned stable signature/DR, stable path, KeepAlive, self_upgrade (in-place execve), the wire protocol, permission-execution points kept; **after the two-section ruling there is no "promote and rename"**.
- **New: ava-root** (a separate program/codebase): the general supervisor; zero permission content; pulled up by the helper (macOS) or the system service manager (Linux/Win).
- `spawn_via_helper`'s "identity = a hard requirement" (loud failure, no fallback) — promoted to I3's guarding assertion.
- The helperproc session path (currently the canary branch) → the final-state main path.

**Retired** (concepts/mechanisms that no longer exist in the final state):
- The `AVA_PERMISSIONS_HELPER_SPAWN` switch + the `permissions_helper_enabled` double switch gate (`shared/session_backend.py:627-644`).
- "Pick a backend once per process" (the `get_backend` singleton) — root is the sole spawn executor; there is no backend-selection concept.
- The "dual track / spare" semantics of posixproc/helperproc/winproc — restated as **root's platform execution implementations** (one contract, three implementations), not a runtime either-or.
- `_reparent`'s double fork (forbidden by I2; zombie prevention moves to root's reaper — E2(b)).
- The lifecycle group among the 9 OS job classes (autostart/health/watchdog-probe/logs) — absorbed by "the adapter pulls up root + root's built-in supervise/logs"; the permission group (helper registration) stays an **adapter** (seeder); the independent service group (gate/LGTM/redis-bridge) is ruled → all into the tree (G6).
- The user-space watchdog daemon and the OS watchdog-probe layer — merged into root (E3).

**Migration = one-shot (big-bang precedent, 2026-08-07)**:
- Coverage: the three-machine deployment (macmini/mba/cm) + wsl + win + **company-air (the company MBA; entered main 2026-09-08 — not part of this inventory round; listed for a draft-2 inventory completion)**, the multi-cluster registry, the helper authorization matrix (the authorization subject = the same helper identity; **promotion does not change the authorization** — the user's Settings touchpoint does not repeat), and the retirement channel for all launchd/crontab/schtasks jobs (the os_* writers' de-registration semantics are kept).
- Order (high level): (1) concept/code promotion (helper→root) (2) per-platform adapters register root (launchd/systemd/SCM) (3) retire the old OS jobs class by class (4) B7 self-proof goes live (5) verification (the F list).
- **Canary and the transition period (an independent decision, listed separately for the user)**: the final-state path **does not need** the helper-spawn canary enabled first (that would be a half-migrated state). "Whether to enable the canary during the transition" = **an independent transition-period decision, not part of this final-state design** — if enabled: same helper identity, **no repeated user authorization** (the popup-experience improvement lands earlier); if skipped: the popup experience persists until the final state lands. #405 presents this to the user separately; this draft only notes it.

## F · The "must be nailed down by measurement" list (empirical hooks for the B/E assumptions)

| # | To measure | Related | Method (candidate) |
|---|---|---|---|
| F1 **(top priority)** | Multi-level inheritance boundary: helper→**root**→unit→child process→toolchain, attribution across **every level** (the whole two-section tree) | I3/E1 | A level-by-level spawn probe chain + tccd AUTHREQ_ATTRIBUTION — **F1 result (2026-09-12): verified through the full chain; see below** |
| F2 | Full enumeration of reparent trigger points, measured (launchd/XPC/daemonize/setsid/terminal close) | I2 | Minimal reproduction per mechanism + responsible sampling — **F2 result (2026-09-12): 16 scenarios, zero effect; the anchor is static at spawn, not a runtime event** |
| F3 | tccd authorization-cache behavior (identity unchanged/changed; whether promotion re-triggers authorization) | I3/migration | Authorize, then promote-and-rebuild processes and measure |
| F4 | Attribution preservation differences of an exec (self_upgrade) mid-chain vs at the root position | B4 | One exec at each position, compared |
| F5 | Final-state avoidance of LWCR/EX_CONFIG state-machine failures (the shape of bootout+bootstrap repair semantics after root-ification) | adapter contract | Fault-injection reproduction |
| F6 | Root-as-reaper zombie behavior (long-run after replacing `_reparent`) | E2(b) | Long-run + high-pressure spawn observation |
| F7 | Measured "survives updates" semantics for PTY hosts inside the tree (exemption-marker behavior) | E2(a) | Verify session survival inside an update window |
| F8 | Tree capacity and patrol cost (hundreds of processes, 60s patrol) | B7 | Measurement + performance |
| F9 | Windows isomorphic landing (logon task→root→units; Job Object replacement) | touchstone | Paper walkthrough + a minimal prototype |
| F10 | WSL shape (converging the systemd-user vs crontab dual track) | edge | wsl measurement |
| F11 | Startup self-check: root verifies it was pulled up by the correct parent (macOS responsible self-check) — **the self-check logic lives adapter-side / or is generalized inside root (without permission symbols)**, see B2a | I2/I6 | Probe-implementation verification |
| F12 | Adapter keepalive path sampling: after launchd/systemd/SCM **restart root**, sample the whole tree's attribution/chain state (macOS is the focus) | I2/adapter contract | Fault injection (kill root) → keepalive pull-up → sampling |

> Ammunition (from #1818): F2/F7 can use the "TTL reclamation kills only the wrapper; the child survives" proof; F6 uses the `_reparent` zombie-prevention historical case; E2(c)'s event-level hard sources (the pidfile accounting fix / mixed-worktree class) are annotated by #1818.

### F1 result (measured 2026-09-12, macmini)

Ran the level-by-level probe chain: helper→L1→L2→L3→L4 (4 fork+exec hops) plus a bash → exec(python) toolchain variant — **27/27 requests held responsible=com.ava.permissions-helper, no broken level** (the 3 orphan probes also held — 30/30 total); a non-helper control chain resolves to its own root (com.apple.sshd-keygen-wrapper), proving the method discriminates. Two boundary findings: (a) **reparent does not reset attribution while the chain root is alive** (double-fork + setsid orphan, ppid=1, still resolves to the helper) — B5's wording is narrowed accordingly; (b) any TCC request to a not-yet-authorized service (e.g. Desktop) **prompts and blocks** — the authorization inventory therefore uses a zero-prompt preflight query probe (G0). Remaining unknowns: the helper-death scenario and the memo TTL (F2/F12), single-run evidence (n=1). Evidence record: workspaces/6127/F1-findings.md.

### F2 result (measured 2026-09-12, macmini)

Full enumeration across 16 scenarios x 3 requests: parent exit / launchd adoption (ppid=1) / setsid / double-fork (hand-written and the real shared._reparent primitive) / SIGHUP single and process-group / PTY close / bash exec — **no trigger changes the attribution resolution**. The helper lineage held responsible=com.ava.permissions-helper 30/30; the non-helper control lineage self-anchored 18/18. Conclusion: the anchor is decided **statically at spawn** by the lineage's spawn-root class, robust to every later topology change; the 2026-09-04 "reparent resets" observation is refuted (it was the non-helper lineage's own anchoring, not an effect of reparent). XPC could not be minimally reproduced within constraints (deferred to F5/F12); the helper-death path remains the only open reset candidate (F12). Zero prompts across the suite. Evidence record: workspaces/6127/F2-findings.md.

## G0 · User touchpoint list (the "you'll need to act" summary along the final-state path)

| # | When | Action | Duration | Notes |
|---|---|---|---|---|
| 1 | Before enablement (once per Mac) | Tick the boxes in System Settings: the helper's Desktop/Documents/Downloads folders + the AppleEvents target | ~1-2 min/machine | 3 machines inventoried (macmini/mba/company-mini) + company-air still to be inventoried (4th candidate, same mechanism); **NOTE: the three folder authorizations have NOT been executed yet** (macmini empirically lacked Desktop access on 2026-09-12); Screen Recording / Accessibility must be re-verified per machine (not assumed); the user must perform it **at that machine** (a TCC prompt may not be visible in a remote session). |
| 2 | Migration window (once per machine) | Confirm a time window (no hands-on needed) | ~0 (one sentence back) | One-shot migration; includes a rollback path |
| 3 | Post-migration acceptance (optional) | One experiential confirmation (popups gone, file access normal) | ~1 min | Can be delegated to an agent to verify automatically; you can skip it |

> **Total: one ~1-2 minute authorization click per Mac** — this touchpoint happens exactly once, whether the canary lands first or the final state does (not cumulative, not repeated); the rest is optional. The Windows/WSL side has no manual touchpoints.

## G · Ruling record and landing tracking
(The rulings = the decision card; this section tracks implementation details)
- G1 two-section — decided. G2 full tree — decided. G3 one tree per (machine × $AVA_HOME) — decided. G4 big-bang — decided. G5 ava-root (the helper keeps its name) — decided. G6 independent service group, all into the tree — decided.
- Fallback strategy = lose attribution, not service — decided (user ruling 17:41; B5 + the two-section crash semantics).
- Landing details (draft-3 / the implementation phase): (1) designing the "zero permission content in root" lint rule (B2a) (2) refining the helper→root keepalive chain and the single-instance flow (E1/B5) (3) aligning the B7 metrics with the F list (4) decomposing the migration steps (D) (5) per-machine audit of the actual helper authorization state on all four Macs — using the zero-prompt preflight query probe (no side effects); never trigger a TCC prompt on a user or company device — added 2026-09-12, after the F1 event (task #3202).

## Open / to verify (tracked)
- [ ] Stale docstrings such as `ops/agents.py:16-17` (already listed as a candidate small task)
- [ ] A line-by-line read of the `machine=` spawn-routing entry (C to verify)
- [ ] The otel-collector supervisor shape (A to verify 3)
- [ ] Refining the G6 counter-case (LGTM staying independent)

---

## Appendix · Workflow and next steps
- Material chain: CTO as the writer; #6124's code-level A/C inventory (read-only, repo@91c6b4735); #1818's ops facts.
- Next steps (the implementation phase): the key F measurements first (F1 multi-level attribution); the PR sequence P1–P7 (synced with #405); the final draft lands in the repo as usual (`future/` + `decisions/`).
- Discipline: the design surface is closed (17:41); the migration/cutover moment is watched by the user in person, and its window is booked separately.

<!-- Translation notes (draft-2 to English, task #3201), for review:
- Glossary choices: "two-section form" for the two-section root topology; "seeder" for the helper's role; "the helper pulls up root" (not "starts"); "reseeding" and "adoption" for the two recovery modes; "attribution" always means TCC attribution.
- Machine names and ids are kept verbatim (macmini, mba, cm, company-mini, company-air; agent/task numbers; the internal workspace paths in A3). Flag if the public tree should not name deployment instances; scrubbing would change the source's facts.
- The root supervisor capability list ("self-upgrade (exec replacement)") comes from an unclear shorthand in the source, interpreted from its parenthetical "(exec upgrade)".
- "three-way readiness" (start's readiness, sections A1/E3/C) is a presumed rendering of the source term; re-checkable in the internal source material.
- The E open items keep the source's literal "draft-2" target marker even though this document is draft-2; left as-is rather than interpreted.
- "big-bang" is kept as the internal name for the one-shot migration (2026-08-07 precedent).
- Emoji and circled digits from the source are rendered as words or plain numbering per repo rules. -->
