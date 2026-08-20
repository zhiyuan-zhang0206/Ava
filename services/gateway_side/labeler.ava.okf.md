---
type: doc
title: Labeler — Agent Auto-Naming
description: Independent agent label auto-generation process — polls per second for rows in `agents` where `label IS NULL AND NOT label_user_set`
tags: []
---

# Labeler — Agent Auto-Naming

## What is it
An independent agent label auto-generation process — polls per second for rows in `agents` where `label IS NULL AND NOT label_user_set`, takes the first chat inbound as prompt, calls LLM to generate a short label name (max 64 characters). Completely decoupled from Gateway.

**Role affiliation**: gateway side (pure agent-runner does not run) — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`; roster derived by `services_for_capabilities` intersecting with local `machine_role()`.

## Core Responsibilities
- **Poll unnamed agents**: SELECT agents needing auto-naming every second
- **LLM label generation**: uses the agent's first chat message as context, calls the LLM from `shared/lm/factory.py`
- **Reject a non-label**: `labeler.py:_rejection_reason()` classifies an output that is not a label at all (opens with a tag or code fence; opens in the assistant's first-person voice; echoes the system prompt back) as a FAILED generation — `generate_label_async` returns `False` and the daemon's existing exponential backoff retries. Rejection, not repair: `_normalize` still refuses to rewrite a bad output
- **Give up on the unlabelable**: after `daemon.py:_GIVE_UP_AFTER_FAILURES` consecutive failures (~28 minutes of retrying) an agent is RETIRED — permanently excluded from the poll `SELECT`, label left NULL, `label_generate_retired` emitted once. Bounds a permanently-failing agent to a fixed number of LLM calls instead of ~12/hour forever. Per-process, like the rest of the backoff state
- **Publish update**: after generating a label, publishes via `shared/labels.publish_label_updated`

## Key Dependencies
- [[db.ava.okf.md]] — reads and writes `agents` table
- [[shared/lm/lm.ava.okf.md]] — LLM call (`build_chat_model`)
- [[loop.ava.okf.md]] — agent labels are used for fleet view display and neighbor discovery

## Entry Points
- `services/labeler/daemon.py` — polling main loop
- `services/labeler/labeler.py:generate_label_async()` — LLM label generation

## Notes
- Label generation logic lives in `shared/labels.py` (labels on agent rows) + `services/labeler/labeler.py` (the generation service) — extracted out of the gateway to eliminate a services → gateway reverse dependency
- System prompt restricts label to ≤ 64 characters, outputting only the label itself
- The prompt being summarized is frequently machine-authored — a long English second-person imperative brief written by one agent to spawn another. That shape steers a summarizer into *executing* the brief; the validity check above is what keeps the result out of the user-facing `agents.label` (issue #178)
