# 0009 — Complexity must name the failure it prevents

**Date:** 2026-09-24 to 2026-09-30
**Anchors:** #3479 (landed as `5b1f71776`, reviewed head `e5aeb863a`), #3661
(retirement of the one-time cutover tooling), `cli/release_fleet/inventory.py`
(`require_fleet_of_one`, `require_topology`), `decisions/2026-09-30-networked-cluster-stays-on-source-updates.md`
(pending when this was written)

## Summary

The unified cluster lifecycle replaced the in-place updater with retained
release images, a frozen image-exec handoff, a fleet coordinator, a new
database write generation per release that fences the previous one, and a
per-unit capability for every remote runner. Landing it took 274 commits, eight
independent review rounds, twelve Linux A→B→A rehearsals, and one fleet
rehearsal. It blocked other development for days. After it landed, production could use none of its release path:

- Networked fleets are refused outright, because the connected adapter supports
  a fleet of one only.
- Two more gaps meant the networked path could never have worked:
  - The coordinator presents the human bearer to a unit's `/ops`, which accepts
    only generation tokens.
  - A unit's finite executor holds no database or API authority.
- Reaching the path needs three further slices, estimated at 2,500–3,500 lines
  plus two VM rehearsals:
  - fleet credentials;
  - schema-changing releases;
  - image adoption for macOS.

Production updates instead run through a script: stop every unit, check out an
exact commit, sync, and start.

The mechanism was not one bug. The design was sized to an idealized threat
model and an idealized end state. It was never compared against prior art or
against the simplest sufficient alternative. Every safety net judged whether the
code matched the design; none judged whether the design had to exist.

The guardrail is a design rule. Before a new subsystem, write down the failures
it must prevent at this system's real scale, the established tools that solve
the same problem, and the simplest sufficient design. Every further mechanism
must name the failure it prevents.

## Timeline

- The lifecycle was designed as a general fleet release system:
  - every transition journaled and resumable after a crash at any point;
  - automatic recovery once, then hold;
  - an immutable, verified image per release;
  - a cross-version handoff contract frozen at v1;
  - database authority split into per-release write generations with fencing,
    so a stale writer is refused by the database itself;
  - per-unit enrollment secrets and capability bundles.
- The branch grew to 274 commits and touched most of the lifecycle, data-plane,
  and CLI surface. The one-time cutover scripts for legacy-born homes lived in
  the same branch.
- Review ran eight rounds: one whole-branch round and seven deltas. Every round
  closed its P0/P1 findings before the next. Linux A→B→A proofs ran twelve times
  on a VM. A fleet rehearsal drove the cutover through its seventh step on a
  legacy-born Linux gateway and a macOS runner VM, and recorded 44 findings.
- Each finding was closed with code and tests before moving on. This held even
  when a person supervising the switch could have handled the case on the spot.
  The operator's acceptance bar treated edge coverage as the definition of
  done.
- The maintainer stopped the rehearsals. The cutover would be supervised live,
  and production data could be corrected by hand if needed.
- The cutover window closed business for 1 h 41 min and recorded 30 findings.
  Four blocked the window; all four were bypassed on the spot, and none needed
  a code change:
  - a legacy login role owned objects;
  - the adoption script booted configuration in-process before popping the
    derived keys;
  - a legacy `post-checkout` hook made a detached checkout recurse forever;
  - a legacy-built helper app was immutable to the new build.
- Planning the first production update found that `release request` refuses
  every networked fleet. Planning the fleet-credentials slice then found the
  image-mode prerequisite, the schema-change prerequisite, and the two gaps
  above. The in-process fleet tests had replaced every remote unit's effects
  with stubs, so neither gap had ever failed a test.
- The maintainer chose to keep production on scripted source-mode updates and
  shelve the networked release chain.

## Root cause

No requirement statement fixed how far the problem had to be solved. The real
need was to update one person's cluster of five machines safely, with that
person present. The design answered a much larger question: a self-healing,
zero-trust fleet release for units that may crash, sleep, or be compromised at
any moment. The threat model grew by accretion:

- a stale writer might exist, so the database fences it by credential;
- a unit might be compromised, so each unit holds its own capability;
- the operator might crash anywhere, so every step journals and resumes.

Each addition was defensible alone. None was asked whether that failure had ever
happened here, or what it would cost to handle it by hand when it did.

Why each safety net missed it:

- **Design review.** No design document had a prior-art section or a
  simplest-alternative section. Nearly every concern has a standard answer:
  - release directories with a switched pointer (Capistrano);
  - push-based orchestration over SSH (Ansible, pyinfra);
  - a schema-version check at startup (Flyway, Alembic);
  - a minimum-supported-version gate against stale clients;
  - expand-contract migrations with old and new code coexisting, which the repo
    already used.

  Fencing tokens come from distributed locking. There a stale leader corrupts
  data. Deployment practice usually assumes compatibility between adjacent
  versions instead.
- **Code review.** Eight rounds asked whether the implementation matched the
  design and was correct. No round had a mandate to ask whether a subsystem
  could be deleted and handled on site instead. Reviews found real bugs, and
  each fix added mechanism.
- **Tests.** The fleet tests stubbed the unit side at the process boundary and
  launchd at the OS boundary. The networked path was green in CI while being
  unreachable on the only topology the project runs. The single-box A→B→A proof
  exercised the fleet of one, which production is not.
- **Rehearsals.** Each round took hours. The feedback loop was too slow to
  question the design, and each round fed more findings back into more code.
- **Process.** The operator agent took over a design another agent had already
  fixed, and set its goal as "land this". It did not re-derive the
  requirements. Sunk cost grew with every round and made the question harder to
  ask.
- **Change shape.** One-time migration code and long-term mechanisms shared one
  pull request. The migration's complexity leaked into long-term code, for
  example a cutover-hold branch in the runtime start path. This multiplied the
  review and rehearsal surface.

## Guardrails added

- `ava_builtins/skills/ava-serious-engineering/practices/design/SKILL.md` gains:
  - a core principle, "Prior Art and the Simplest Sufficient Design First";
  - a checklist item;
  - an anti-pattern, "Reinventing the Commodity Layer".
- `conventions/defensive-patterns.md` gains a "Design and scope" section with
  "Complexity must name the failure it prevents".
- Unguarded: no lint or test can enforce any of this. It relies on design
  documents and reviews applying the rule. A full-repository audit against the
  same rule was commissioned from an Ava agent. Its findings are the input for
  deciding what else to delete.

## Lessons

- Write down how far a problem must be solved, at the system's real scale,
  before designing it. A threat model that is not written down grows by
  accretion.
- For a commodity concern, start from how established tools solve it. Deviate
  only for a constraint that is specific to this system and stated in writing.
- Every mechanism must name the failure it prevents: one that has happened, or
  a credible one that the simplest design cannot handle by hand when it occurs.
  A mechanism that cannot name one is cost.
- Make the simplest path run end to end on the real topology before adding
  breadth. A path exercised only through stubs has not been built.
- Review for necessity as well as correctness: can this be deleted, and what
  would handle the case on site?
- When taking over a design, re-derive its requirements before continuing it.
- Keep one-time migrations out of the long-term change, and delete them after
  use.

Distilled into
[`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md#complexity-must-name-the-failure-it-prevents).
