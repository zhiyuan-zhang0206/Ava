---
type: doc
title: Labeler — Agent Auto-Naming
description: Independent agent label generation with one attempt per process and explicit service failures
tags: []
---

# Labeler — Agent Auto-Naming

## What is it
An independent agent label auto-generation process — polls per second for rows in `agents` where the label is NULL or empty and not user-owned, takes the first chat or task-system-note inbound as prompt, calls LLM to generate a short label name (max `services.labeler_max_chars` characters, default 64). Completely decoupled from Gateway.

**Role affiliation**: gateway side (pure agent-runner does not run) — `ServiceSpec.capabilities=GATEWAY` in `services/supervision/roster/spec.py`; roster derived by `services_for_capabilities` intersecting with local `machine_role()`.

## Core Responsibilities
- **Poll unnamed agents**: SELECT agents needing auto-naming every second
- **LLM label generation**: uses the agent's first chat or task-system-note message as context, calls the LLM from `base/lm/factory.py`
- **Reject a non-label**: `services/derived/labeler/labeler.py:rejection_reason()` classifies an output that is not a label at all (opens with a tag or code fence; opens in the assistant's first-person voice; echoes the system prompt back) as a FAILED generation — `generate_label_async` returns `False` and the daemon leaves the label unset. Rejection, not repair: `_normalize` still refuses to rewrite a bad output
- **Expected provider failures**: only a trusted transient error from the actual `ainvoke` call returns `False`: SDK / official LangChain transport errors, HTTP 429 and the shared temporary-status contract. Raw HTTP timeout/connection failures are normalized only at that invocation boundary. Unknown and permanent provider failures retain their original exception; model construction, usage accounting and parsing are outside recovery.
- **Service failure owner**: exceptions from generation, CAS or polling propagate through `run` cleanup to `main`, which records a traceback and exits nonzero. The current batch stops; no task is claimed or acknowledged. The root health scheduler owns service replacement. Restart clears the set of attempted rows, so an unset label may be attempted once again. No retry or retirement state is persisted. No new DB retry policy is assumed from `OperationalError` or `PoolTimeout`.
- **One attempt per process**: each eligible agent is recorded before generation and excluded from subsequent polls. A false generation result logs a warning and leaves the label unset; there is no backoff, retry ladder or retirement threshold. Fresh agents remain eligible.
- **Configuration** (`config.py`, `daemon.py`): `labeler_config()` in the daemon (the package's composition root, the only module there that reads `settings`) builds the frozen `LabelerConfig` (`labeler_model`, `labeler_max_chars`); the dispatch loop and `generate_label_async` take it as an argument alongside the explicit model catalog, override and model settings.
- **Model inputs**: the daemon builds one explicit catalog and freezes caller tuning through `labeler_model_overrides`. Gateway-profile calls leave agent-only tuning unset, retaining model defaults without reading stripped aliases. A full or agent-side boot retains its explicit runtime tuning. Both paths keep the existing disabled-thinking request and pass the same catalog through construction and usage accounting.
- **Publish update**: after the label CAS commits, publishes via `base/agents/labels.py:publish_label_updated`. This live Redis notification remains best-effort; there is no receipt or outbox. A notification failure cannot roll back the label, and a non-empty committed label is not polled for notification replay. The shared EventBus best-effort boundary retains its own policy.
- **Process image and DB admission**: the executable captures one `LoadedCommit` before the schema check. Logging and `/healthz` retain that same image; unknown stays unknown. One `CodeVersion` and `ProcessDbGate` admit both the producer's fresh database handles and the dispatch database. The entry supplies this factory to `run`; pidfile and health publication still precede pool opening. Schema checks, terminal failures and bounded shutdown retain their existing order.

## Key Dependencies
- [[agent/db/docs/db.ava.okf.md]] — reads and writes `agents` table
- [[base/lm/docs/lm/lm.ava.okf.md]] — LLM call (`build_chat_model`)
- [[loop.ava.okf.md]] — agent labels are used for fleet view display and neighbor discovery

## Entry Points
- `services/derived/labeler/daemon.py` — polling main loop
- `services/derived/labeler/labeler.py:generate_label_async()` — LLM label generation

## Notes
- Label generation logic lives in `base/agents/labels.py` (labels on agent rows) + `services/derived/labeler/labeler.py` (the generation service) — extracted out of the gateway to eliminate a services → gateway reverse dependency
- System prompt restricts label to `services.labeler_max_chars` (default 64) characters, outputting only the label itself
- The prompt being summarized is frequently machine-authored — a long English second-person imperative brief written by one agent to spawn another. That shape steers a summarizer into *executing* the brief; the validity check above is what keeps the result out of the user-facing `agents.label` (issue #178)

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.
