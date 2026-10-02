# `ava-root` stays self-written: the five-question record

## Context

[`2026-09-12-process-lifecycle-final-state`](2026-09-12-process-lifecycle-final-state.md)
chose a self-written supervisor, `ava-root`, under the signed permissions helper on macOS.
It was decided before [`conventions/technology-selection.md`](../conventions/technology-selection.md)
existed, so it carries no five-question record, and the retrospective of 2026-10-02 found no
comparison with a mature supervisor anywhere in `decisions/`, `future/`, `conventions/`,
`postmortems/` or the OKF nodes. The owner's ruling is to keep it and not reopen it; this
entry only writes the answers down.

## Decision

Keep `ava-root`. No change of design or code. The five questions:

1. **Boundary.** `ava-root` owns one full process tree per home: services, the agent host and
   PTY hosts, with custody recorded before a spawn, bounded stop with proof, single instance,
   in-place exec for its own upgrade, a sealed manifest checked before every native birth,
   and a tree self-check. It owns no permission content (a lint blocks any permission-domain
   symbol in its code) and does not own the data plane, whose custody is separate so that an
   application shutdown can keep the database.
2. **Prior art.** Process supervision is a solved problem: systemd, launchd, and portable
   supervisors (supervisord, s6, runit, circus). Platform supervisors are rung 1; a portable
   supervisor under the helper is rung 3; `ava-root` is rung 4. The primary reason `ava-root`
   exists, as the owner stated on 2026-10-02: **on macOS the supervised processes must inherit
   the helper's permissions.** TCC attributes a permission to the responsible process fixed at
   spawn, so every process that needs one must descend from the one authorized, signed helper,
   and launchd cannot carry that chain per service. That forces a supervisor layer under the
   helper. Whether it must be self-written is a separate question: the 2026-09-12 attribution
   probes held the helper's attribution across four fork-exec hops (27 of 27 requests) and
   across sixteen re-parenting scenarios, which suggests a mature supervisor started by the
   helper would inherit it too. That is an observation about the chain, not a comparison of
   supervisors: none was ever run, and no conclusion is drawn from it. The semantics below are
   the secondary reason for the self-written form.
3. **Simplest option.** Linux alone: one systemd unit per service. macOS plus Linux with one
   OS-pure root: a portable supervisor under the helper, plus whatever custody and sealing
   Ava adds on top. The first is simpler per platform but gives two lifecycle models; the
   second removes the supervision loop but not the semantics Ava needs.
4. **Failure prevented, and what we now own.** Prevented: four or five stacked ad-hoc layers
   (launchd as attribution root, keepalive and scheduler at once; nursery, watchdog, restarter,
   session server) with a chain that truncates and an owner that is conceptually dangling.
   Owned by us because no off-the-shelf supervisor has them: custody-before-spawn, stop
   proof (a failed stop keeps custody and blocks replacement), the sealed manifest and launch
   digest, owner-bound native birth identity, exec replacement of the supervisor itself, and
   a tree self-check that tells "broken" from "cannot verify". Production code is about
   3,900 lines with about 2,300 lines of tests (2026-10-02 count), plus the independent
   helper program. Chain integrity is checked at runtime; attribution transfer is not
   measured at runtime and is proven only by the CI two-section chain smoke. The self-check's
   attribution-coverage and reseeding-latency metric slots never had a data source and were
   removed on 2026-10-02; F12 (attribution sampling after a keepalive restart) remains a
   measurement debt.
5. **Limit signal and exit.** See the next section.

## Why it stays

- **Primary (owner, 2026-10-02):** on macOS the supervised processes must inherit the
  helper's permissions; the supervisor layer under the helper exists for that.
- **Secondary:** the owner's 2026-09-12 stated motive was that the architecture looked badly
  coupled to operating-system-specific machinery; the design is the decoupling (an OS-pure
  root, helper kept separate). That is a preference, not an external constraint, and it is
  the owner's to keep. Custody, stop proof and the sealed manifest are semantics a mature
  supervisor does not ship, and part of why the layer is self-written.
- The design is deliberate rather than accreted: measured attribution, an enforced lint on
  scope, a postmortem-backed guard on a supervisor replacing itself.
- Replacing it now would be a second one-shot migration of the whole lifecycle for a cost
  that is not hurting.

## Stop-loss: when to reopen

Qualitative triggers, none a number:

- A **defect class in the supervision core recurs** (stop proof, custody, exec replacement,
  restart loops) in a way a mature supervisor would not have produced.
- **Upkeep costs more than replacing it**, or a second consumer appears that needs the same
  machinery outside this tree.
- The **platform constraint changes**: a mature supervisor or the platform itself can carry
  the helper's attribution chain, so the layer's reason shrinks to the Ava-specific semantics.
- **Attribution loss is observed in practice** (a process losing its permission after a
  restart), which the CI smoke alone would not have caught: then F12 stops being a debt and
  becomes the first thing to measure.

Exit: a portable supervisor under the helper, keeping only the Ava-specific semantics
(custody, sealing, stop proof) as a thin layer, or per-service systemd on Linux where the
platform covers it.

## Alternatives rejected

- **Per-service launchd jobs.** The attribution chain cannot be carried per service.
- **Helper as root.** Rejected by the owner on 2026-09-12; see the lifecycle decision.
- **A portable supervisor under the helper.** Not rejected on merit and never compared in a
  record; the named exit.
- **Kubernetes-style semantics.** Rejected on structural mismatch in
  [`2026-07-19-ops-k8s-semantics-without-k8s`](2026-07-19-ops-k8s-semantics-without-k8s.md).

## Consequences

- The lifecycle decision stays authoritative; this record adds the answers it lacked.
- The runtime guard for attribution loss does not exist; the design relies on the CI smoke
  and on permission errors surfacing. F12 is the recorded debt.
