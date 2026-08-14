# Ava roadmap

The live, ordered list of what Ava is going to build, distilled from the
2026-06 competitor benchmark after a per-item alignment pass with the user. The
benchmark is the raw recon; this folder is the actionable extract.

The point of this doc is the **buckets**, not a Gantt chart. An item's bucket
encodes *why it is or isn't being worked on now*, which is the thing that drifts
if it lives only in someone's head.

This folder is the **strategic capability/product roadmap**, distinct from the
engineering backlogs in [`../infra/`](../infra/) and the module-co-located plans
(`agent/`, `ops/`, `shared/lm/`, `ava_builtins/`) —
those track build-level work item by item; this tracks what Ava is *for* and what
it builds next at the capability level, and links down into those streams where a
strategic item has a build-level design.

## Guiding principles (read before adding an item)

1. **Five-primitive reduction first.** Before anything becomes a framework/SDK
   feature, test it against the five primitives (MCP / skill / peer agent /
   inbound-event / LLM-composed Python). If any resolves it, the framework adds
   nothing — it is *already done by composition*, not a gap. This is why most of
   the benchmark's "competitor ships feature X" rows are not roadmap items.
2. **SDK calls are synchronous and return fast.** The SDK surface is for quick,
   synchronous operations. Long-running or complex work does not get an async
   task mechanism inside the SDK — it goes into a separate Python script the
   agent runs (`ava.watcher.launch` / a standalone process). Do not grow the SDK
   toward a job/queue/async-result API.
3. **Small core, strippable.** Every item is judged on whether it can later be
   removed as the model gets stronger. UI surfaces and skills are strippable;
   the sandbox/persistence/wake substrate is not.
4. **The working loop: brainstorm -> fan out parallel directions -> converge.**
   Requirements are thought up first, split into a few directions run in
   parallel, then collected back. This holds at both levels today — human <->
   Claude and human <-> Ava — and the large-scale-orchestration item
   ([`large-scale-orchestration.md`](large-scale-orchestration.md)) is just this
   same loop run by an agent over many agents. Framed as **current-capability-
   bounded**, not an eternal law: it is what today's agent strength affords, and
   it is expected to simplify as agents get stronger (strippable, like
   everything else under principle 3).

## North stars — the biggest ambitions (not urgent, never forget)

Two, at different layers — the application is the ultimate "why," the capability
is how the system gets good enough to reach it:

| Layer | North star | Detail |
|---|---|---|
| **Application** | A multi-agent framework that autonomously runs an **online business / one-person company** and **makes money** — the thing people fake today by hand-managing a few Claude Code sessions, with no real framework underneath | — |
| **Capability** | **Autonomous self_evolution** — one machine (observe -> propose -> guardrail -> promote) over escalating targets: **gen 1** curates skill/memory *text* and files design issues against its own repo (the half competitors ship; buildable now, no sandbox gate) and **gen 2** rewrites the *codebase* itself (the deeper half competitors don't reach; gated on the sandbox + eval fuse) | gen 1 [`autonomous-learning-loop.md`](autonomous-learning-loop.md) · gen 2 [`self-code-evolution.md`](self-code-evolution.md) |

## Now — near-term build

| # | Item | Shape | Detail |
|---|---|---|---|
| 1 | **Task-intake skill** — brainstorm -> search solutions -> scope -> pin a success criterion | general skill (CC + Ava), built on superpowers `brainstorming`; makes captured tasks eval-ready | [`evaluation.md`](evaluation.md) |
| 2 | **Docker isolation** — a disposable containerized cluster (mostly already in the test rig) | wire it as the eval / self_evolution substrate + promote-on-pass | [`docker-sandbox.md`](docker-sandbox.md) |
| 3 | **Fleet-board controls** — batch / subtree kill, a cross-agent message panel, a Ctrl+K palette over the lifecycle actions | the board itself shipped (`/fleet`: lineage graph + activity/label + task tree + notice queue); what is left is acting on many agents from it, not seeing them | [`large-scale-orchestration.md`](large-scale-orchestration.md) |
| 4 | **Import an arriving user's agent history** — a one-command demo that distills their Claude Code / Codex history into the memory pool, so Ava starts knowing their setup instead of at zero | demo script over the existing SDK (no framework code); gated on going public — it pays off once there are new users to onboard | [`../../agent/import-existing-agent-history.md`](../../agent/import-existing-agent-history.md) |
| 5 | **Autonomous learning loop (gen 1 of self_evolution)** — in-moment "plan for the future" notice/jot + a weekly forked Curator that triages into skills / memory / repo issues / PRs and prunes the library | framework delta = one always-on prompt section (+ optional `skill_loaded` event); rest is skills + watchers; **not** sandbox-gated (text is git-reversible, issues are human-triaged) | [`autonomous-learning-loop.md`](autonomous-learning-loop.md) |

(Evaluation as a whole is big and coupled with #2 — but its hard part, task
capture, reduces to the #1 intake skill; what's left is replay plumbing. #5
*consumes* #1's success criterion as its grading rubric and emits captured tasks
as the replayable eval set, so #1 -> #5 -> evaluation close a loop. The
orchestration *leader skill* is intentionally **not** here — it emerges from real
work, see its doc.)

## Gated on the Docker sandbox landing

| Item | Why gated |
|---|---|
| Permissions / approval model | Token-auth + approval routing gain real defensive power only once code is confined. Before a boundary, any in-process check is bypassable, so it is pure cost. Builds *with* the sandbox, not before. Likely shape: **container-level capability boundaries, not per-tool allow/deny prompts** (the latter is the banned framework-babysit pattern). |
| Autonomous self-code-evolution (north star, **gen 2**) | Gated on the sandbox *and* the eval harness — an agent rewriting its own framework needs both a blast-radius boundary and an objective scorecard before the loop is safe to close. (Gen 1, the skill/memory-text learning loop, is **not** gated — it is "Now" #5; the fuse here is the *code* target only.) |

## Open-source / scale-out prerequisites (deferred, not rejected)

Ava is single-user / single-host today, which is why these are not built. They
become required at the moment the project is open-sourced and grown for
adoption — the reasons they are "no" now are positioning, not architecture, so
they flip when positioning flips. Captured so they are not mistaken for
permanent noes. Detail: [`open-source-prerequisites.md`](open-source-prerequisites.md).

| Item | Why it matters at open-source time |
|---|---|
| 20+ IM ingress channels (Slack/Discord/Telegram/WhatsApp/WeChat/...) | Users live in their favorite IM app; native ingress is table stakes for adoption + GitHub stars. **In progress**: `im_bridge` v1 ships Telegram / WeChat (iLink) / Feishu as gateway services; the adapter pattern makes every additional IM a new adapter. Detail: [`im-frontends.md`](im-frontends.md) — issue #971. |
| Provider fallback chain (DeepSeek -> Kimi/Qwen/Anthropic) | A public project cannot hard-fail when one provider is down; fallback becomes a real requirement, not a model-babysit. |

## Low priority / opportunistic

| Item | Status | Note |
|---|---|---|
| MediaGen skill | skill, not framework | No current need. When wanted: a skill carrying AIGC API templates (image/video/TTS) for the major providers — keys already exist, the only work is the templates. Pure primitive 2 + 5. |
| Session / transcript search | nice-to-have | Largely subsumed by the `/fleet` board, which surfaces activity + label; revisit only if search is still missing after #1. |
| MCP-serve (expose Ava as an MCP server) | minor todo | The gateway port is already the control surface, so this is not needed. Trivial if ever wanted — tracked only so it is a decision, not a silent hole. |
| Live mid-session model / effort switch | minor todo | Today `llm_model` + reasoning-effort are restart-gated (respawn via the per-agent config overlay), no in-flight swap. Pure ergonomics; not important. |
| `ava doctor` (guided diagnose + fix) | minor todo | `ava status` diagnoses, `ava converge` repairs on every start; a guided `doctor --fix` is a thin wrapper over the existing probes, only if onboarding friction ever surfaces. |
| Anthropic prompt-caching / context-editing API | gated | Deliberate-no until the gateway model moves to Anthropic; DeepSeek server-side auto-cache is observably sufficient today. |

## Deliberate-no (permanent, architectural)

Not a backlog — these are decided. Listed so they are not re-litigated.

| Item | Reason |
|---|---|
| **ACP / editor-driven integration** | Editor-driven agents are a sunset paradigm — agents are going native/top-level, which is exactly Ava's positioning. Implementing the editor<->agent protocol would invest in the losing side. |
| Bespoke IDE/editor plugins (VS Code/JetBrains) | Same reason as ACP; Ava is not an in-editor pair-programmer. |
| Voice / Talk / realtime audio | Low value for a web-primary detached-agent fleet. Reducible to an STT/TTS MCP if ever wanted, but declined by positioning. |
| Composer file-URL `@`-mention | The model is strong enough to locate files itself; current file-upload suffices. |
| Plan/Effort/YOLO **mode state machine** | A "mode" that is really a workflow is a skill, not framework state. Code-Mode is Ava's single permanent mode. |
| In-tree browser (playwright/Selenium) | Chrome MCP already covers browser automation (primitive 1); keep the framework browser-agnostic. |
| Plugin marketplace / remote install registry | Distribution reduces to peer-agent self-provision (file-write + restart, fanned across machines by `spawn(machine=)`). Local-dir install is intentional. |
| Framework "babysit the model" surfaces — user-config lifecycle-hook dispatch, command parser/dispatcher, framework Goal-Mode layer, smart-approvals that "learn safe commands" | All collapse to existing primitives + the fail-fast stance. Building them re-introduces the framework-side dispatch the small-core charter rejects. |

## What changed vs the benchmark's verdicts

The benchmark's §3 reduction verdicts were largely right; the earlier worry that
"reduces-to-primitive was being oversold as covered" mostly did not survive the
alignment pass — under Ava's architecture, "expressible by clean composition of
existing primitives" *is* done, not a gap (e.g. lifecycle hooks = subscribe a
Redis listener / watcher peer to an already-emitted event; goal mode = the
shipped `ava_builtins/skills/ava-goal`; sub-agent result hand-off = a message plus the shared
filesystem). The genuine roadmap is therefore small: the items above, not the
33-dimension matrix. Of the two real UI/infra gaps the benchmark surfaced, the
agent dashboard has since shipped as `/fleet` (only its batch-control layer is
still open, #3) and the sandbox is still in **Now**; the rest is either
done-by-composition, deferred-to-open-source, or a decided no.
