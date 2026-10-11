# A WSL2 unit auto-defaults off the shared health-port block

## Context

[2026-07-31-a-health-port-belongs-to-a-unit.md](2026-07-31-a-health-port-belongs-to-a-unit.md)
fixed the 2026-07-26 WSL2/native-Windows collision by making a health port a
per-unit fact: the operator states a co-located unit's own block with `ava
enroll --health-port-base <N>`, and `ava start` refuses to launch onto a port
another unit already answers on. Omitting the flag leaves every daemon on the
shared default (`shared.daemon_health.DEFAULT_PORTS`, 8102-8109).

Issue #1152: the win/wsl pair on a Windows laptop (`C:\Users\ava\.ava` +
`/home/ava/.ava`) still sits on that shared default today — the collision is
latent because WSL2's default NAT networking does not forward Windows'
loopback into the distro, so the pre-bind gate reads clean on both sides. It
stops being latent the moment WSL2 switches to *mirrored* networking
(`.wslconfig`: `networkingMode=mirrored`), which shares the port space in both
directions and turns the same collision the 2026-07-31 decision fixed for an
explicit choice into an immediate hard refusal for a co-located pair that never
made one. Nothing about the current design prompts an operator to make that
choice before the switch — the flag exists, is documented, and is not read
until something breaks.

## Decision

`ava enroll`, when `--health-port-base` is omitted, checks two things before
falling through to the shared default:

1. Does this unit's `.env` already carry a health-port block (a prior explicit
   `--health-port-base`, or a prior auto-default)? Restore it verbatim on this
   enroll instead of losing it — see "Found and fixed along the way" below.
2. Otherwise, is this host WSL2 (`shared.platform.IS_WSL`)? Auto-apply
   `shared.env_keys.WSL_DEFAULT_HEALTH_PORT_BASE` — a **fixed constant**, one
   slot past the birth allocator's own grid (`BLOCK_MAX + BLOCK_SIZE` =
   20016) — instead of the shared 8102-8109 default a co-located native
   Windows unit would also fall into.

An explicit `--health-port-base` always wins over both. Non-WSL2 hosts are
untouched: the shared default is exactly what an omitted flag produces today,
so single-box and split (different-machine) topologies see no behaviour
change.

### Why this is not the alternative the 2026-07-31 decision rejected

That decision's "Alternatives rejected" explicitly ruled out **"have enroll
pick a free base automatically"** — a scan that probes which ports currently
read free and picks one: "a port that probes free at enroll can be taken later
by a WSL2 boot, and the two sides of a relay do not observe the same 'free'."
That objection is about *dynamic* discovery — a snapshot that goes stale.

`WSL_DEFAULT_HEALTH_PORT_BASE` is not a scan. It is a compile-time constant,
identical for every WSL2 host, chosen once and never re-derived — structurally
the same kind of fact as the 8102-8109 default it replaces for this one
platform, not a new mechanism. Nothing is probed for being free "right now";
nothing can go stale between enroll and a later boot, because nothing was
observed in the first place. The distinction the 2026-07-31 decision drew
(operator-stated fact vs. scanned snapshot) still holds — this is a second
operator-independent *fact*, not a second scan.

## Alternatives rejected

**Reserve a base inside the birth allocator's own grid
(`[BLOCK_START, BLOCK_MAX)`, i.e. `[18000, 20000)`).** A cluster later born on
the same WSL2 box (`cluster.allocate_ports`, which scans exactly that range)
could eventually claim the identical base, silently colliding a dev cluster's
port block with this unit's health ports. Placed one slot past `BLOCK_MAX`
instead, where the allocator never scans (asserted by
`test_wsl_default_health_port_base_cannot_collide_with_a_birthed_cluster`).

**Detect co-location at `ava start` and shift ports only then.** By the time
`ava start` runs, the daemons are about to bind — moving ports at that point
needs the same kind of live reallocation the 2026-07-31 decision ruled out,
and it still would not help whichever unit of the pair starts first, since
there is nothing yet to detect against.

**A louder warning at enroll, no default change.** Does not achieve
"co-located win/wsl do not collide by default" — it only reduces, not
eliminates, the chance an operator notices in time. #1152 was filed
specifically because the existing documented remedy (pass the flag) was never
applied on the live pair it describes; a warning has the identical failure
mode the issue reports.

**Extend the auto-default to every platform, not just WSL2.** Rejected: a
native Windows unit, standalone Linux/macOS box, and every split-topology
runner already get non-colliding ports for free (different machines, disjoint
loopback namespaces) — moving them off the shared default would change
established behaviour for populations that were never the problem, for no
benefit. WSL2 is the one platform whose whole reason for existing is sharing a
loopback namespace with something else.

## Consequences

- A fresh WSL2 enroll with no flag no longer shares the legacy 8102-8109
  bucket with a co-located native Windows unit, in either enrollment order. A
  WSL2 host that is not actually co-located with anything gets a harmless,
  otherwise-unused alternate block — no functional difference for it.
- The reserved base is one fixed value. A hypothetical *third* co-located unit
  (two WSL2 distros on one box, WSL2 + a container) collides on it exactly as
  today's shared default collides on 8100s for a second unit — caught by the
  same `ava start` pre-bind gate, unchanged from the 2026-07-31 decision.
  `--health-port-base` remains the only way to pick a base beyond "the shared
  default" or "the WSL2 default".
- **Found and fixed along the way:** `write_bootstrap_env` replaces a unit's
  whole `.env` on every enroll call, including a bare re-enroll run only to
  rotate `--cluster-secret` or `--machine-host`. Before this change, nothing
  restored a health-port block on such a call unless `--health-port-base` was
  repeated — reproduced directly (`.venv/bin/python` against `cli.enroll`
  twice, first with the flag, then without): the second call silently dropped
  the block the first call wrote. This contradicted the 2026-07-31 decision's
  stated consequence ("an already-enrolled runner keeps whatever ports its
  `.env` holds"), which held for `ava start`'s refresh
  (`materialize_cluster_env`, upsert-only) but not for a second `ava enroll`
  call. `run_enroll` now reads any existing block before the replace and
  restores it verbatim when this call states nothing new — closing the gap for
  every platform, not only WSL2.
