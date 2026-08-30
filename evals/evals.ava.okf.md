---
type: doc
title: "evals"
description: "Shared eval-case contract (schema v1 JSONL) and its reference loader — the Ava-side implementation of the cross-line evalset base (CTO top design item 2)."
tags:
  - evaluation
  - schema
  - shared-infra
---

# evals

## What it is

`evals/` is the Ava-side reference implementation of the cross-line eval-case
base from the shared AI infra top design (item 2, "Evaluation / evalset base"): one
JSONL case format, one loader contract, and the versioning discipline. The
contract is shared; case content stays per line — Ava evaluates agent
behavior, Monsora evaluates product function, and the two never mix in one
evalset file.

- [SCHEMA-v1.md](SCHEMA-v1.md) — the contract: row fields, field rules,
  versioning, and the runner pattern.
- `loader.py` — the reference loader (`load_evalset`), with loud validation so
  a malformed row fails at load time, not mid-run.
- `cases/<domain>/` — evalset files, one domain per directory.

The exchange format is JSONL only. At runtime each line loads its own
native case structure (Ava maps rows to `schedules/adversarial_eval_cases`
`CaseDefinition`-compatible records); the JSONL never drives execution
semantics directly.

## Versioning

Evalsets live in git and change through PRs (user asset ruling). The schema
evolves add-only: fields may be added, never renamed or removed, so an older
loader keeps parsing a newer file (`meta.schema_version` gates the shape).

## Key dependencies

- [schedules](../schedules/README.md) — the adversarial weekly batch whose
  case machinery the migrated example dataset references.
- [agent_eval.py](../shared/config/agent_eval.py) — isolation switches every
  line's runner reuses (eval isolation, network allowlist, container exec).

## Entry points

- `load_evalset(Path)` — parse and validate one `.jsonl` evalset.
- `evals/cases/adversarial/adversarial-weekly-v1.jsonl` — the migrated
  reference dataset (Ava cases c001–c009), the canonical example for other
  lines to copy the shape from.
