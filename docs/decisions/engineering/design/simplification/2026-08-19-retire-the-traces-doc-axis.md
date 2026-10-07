---
type: decision
title: Retire the traces/ doc axis — query the run, do not commit it
description: Ruling 2026-08-19: the traces/ documentation axis is retired. No run artifact is committed to the repo; the evidence it copied lives queryable and current in checkpoints + Loki + Tempo, and the missing piece — the correlation know-how — becomes the inspect-a-trace skill. CI-replayed trace fixtures were rejected.
tags: [docs, observability, traces, skills, o11y]
date: 2026-08-19
status: accepted
---

# Retire the `traces/` doc axis — query the run, do not commit it

Closes issue #44.

## Context

`traces/` was the fifth documentation axis: "what the system **does**, in time —
one real recorded run, annotated, every step anchored". It was meant to hold the
thing the other four axes structurally cannot — a concrete end-to-end run,
rather than a description of structure, a rationale, a plan, or a rule.

By 2026-08 the axis had accumulated more machinery than content:

- A committed run is a fact about a version that has already moved. Every CLI
  verb rename and every behavior change demanded human re-verification of every
  runnable block in every trace. The axis carried a standing record-time
  CLI-shape disclaimer header for exactly this reason, plus a
  `lint_trace_anchors.py` guard over its `file:symbol` anchors, plus blanket
  exemptions in `check_doc_references.py` and `lint_no_emoji.py`.
- The public repo's `traces/` was empty at the cutover. The maintenance
  apparatus outlived the documents it was built for.
- The substance was never really in the document. It was in the stack: Tempo
  spans (OpenLLMetry auto-instruments the LLM SDKs and LangChain/LangGraph;
  metadata-only since trace v2, with turn content resolved on demand from the
  checkpoints table by trace id), the unified event river in Loki, and the local
  OTLP/JSON mirror under `$AVA_HOME/traces/`. A trace document copied a
  snapshot of that and then began to rot; the original stayed current.

What was actually missing was never a document. It was **know-how**: no single
place said how to get from "agent 3048 misbehaved around 14:00" to that turn's
messages, its event stream, and its span tree — which ids join which surfaces,
which query dialect each one speaks.

## Decision

1. **The axis is retired.** Four axes remain (`*.ava.okf.md` / `decisions/` /
   `future/` / `conventions/`), and nothing trace-shaped is committed to this
   repo. The `traces/` exemptions come out of `check_doc_references.py`,
   `lint_no_emoji.py`, and `shared/repo_change.py`'s doc-root set.
2. **The know-how becomes a skill**: `.agents/skills/inspect-a-trace/` — one
   corpus serving both the repo-side developer and the runtime agent (converge
   syncs `.agents/skills/` into the load directory, so `ava.help` reaches it).
   It documents, with runnable queries, the four surfaces and the two ids that
   join them: find the run (checkpoints SQL, including reading an agent's
   history across compaction segments), read the event stream (the
   `{service_name="unknown_service"} | json` LogQL dialect), read the spans
   (TraceQL plus the mirror), and hand the user a Grafana link. The
   `ava-trace-toolchain` skill is folded into it rather than kept beside it —
   its three scripts are the fetch/read/render arm of the same job.
3. **The self-evolution weekly loop references it** as the optional deep-dive
   when a finding needs evidence the dataset transcript cannot carry.

Doctrine this rests on: the Loki event river is the primary observation surface
for a long-running agent, because an agent's life is unbounded and only the
river spans it. Tempo is drill-down for a bounded unit — one turn — reached
once you already know which turn matters.

## Alternatives considered

**CI-ized snapshot replay** (record/replay fixtures, dsh-style), the original
proposal from the DeepSeek Harness comparison. Rejected by ruling 2026-08-19:
real trace artifacts are large, committed fixtures overlap what pytest already
owns, and the actual need — a developer or the self-evolution loop inspecting
one run, on demand, against the version currently running — is served by a skill
invocation rather than a merge gate. Trace inspection is an on-demand operation,
not a CI gate.

**Keep the axis and enforce freshness harder.** Pays a permanent
re-verification cost on documents whose evidence already exists elsewhere,
queryable and always current. The failure mode is not under-enforcement; it is
that the artifact is a copy.

**Dashboards only, no skill.** Dashboards answer pre-shaped questions well and
run-level questions not at all. Debugging one bad run needs the correlation
recipes — `trace_id` ↔ checkpoint ↔ Loki line — which is precisely what a skill
can teach an agent to compose and a dashboard cannot.

## Consequences

- No repo-side record of a historical run survives a version bump. That is the
  point, and the cost is real: a run older than Loki's 168h retention, or whose
  checkpoint the reaper trimmed, is not recoverable. Compaction-boundary
  checkpoints are the deliberate exception — they are preserved, so an agent's
  past segments stay readable.
- Observability surface changes (a renamed event field, a changed span
  attribute) now have a documentation reconcile target: the skill's reference
  pages. `conventions/doc-maintenance.md` records that mapping.
- A generalizable finding from a run still gets written down — but on the axis
  that owns it: a structural fact in the co-located OKF node, a rule in
  `conventions/`, a rejected alternative here.
