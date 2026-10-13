---
type: doc
title: Skill Recall — Offering the Skills That Fit the Request
description: "How a person's message or a schedule trigger gets a system note naming the skills that clearly fit it: one decision-model request scores the whole skill index, the per-window record keeps a skill from being offered twice, and every failure degrades to no note."
tags:
- agent
- skills
- hooks
- decision
---

# Skill recall

The `# Capabilities` index asks the agent to match every task against it by hand, and an index of hundreds of skills is easy to skim past. Skill recall is the push side of it: before a turn woken by new inbound, one decision-model request scores every skill in the agent's index against the recent conversation, and the best matches are named in one system note (`NoteTag.SKILL_RECALL`). It is a sibling of passive memory recall ([[ava_builtins/plugins/ava_memory/docs/memory-recall.ava.okf.md]]) and independent of it: its own switch, model, deadline and record.

## When it fires

`agent/graph/hooks/skill_recall.py:SkillRecallHook` is a framework `before_llm` hook, registered after compact's reminder and before the capability-index drift check (drift stays last). It fires when the messages added since the last assistant turn include an inbound from a **person** (`user`, `ui:page:<name>`) or a **schedule** (`schedule:<id>`); `agent/messages/__init__.py:tail_has_skill_recall_inbound` is the gate. A peer agent (`agent:`), a system notice, and watcher or shell completions never trigger it, and the wider gate memory recall uses (`tail_has_recallable_inbound`) is untouched. It also stays quiet in an eval-isolated run and on a turn where auto-compact is predicted (the note would be wiped with the history).

## What is asked

`agent/graph/recall/skill_recall.py` sends **one** `decide()` request (`usage_kind="skill_recall"`, billed to the agent):

- **State** (sent once): a fixed preamble plus the recent conversation, built by `agent/graph/recall/conversation_query.py:build_conversation_query` (the last 6 real dialogue messages, at most 2000 characters), which is also the query memory recall searches with.
- **Questions** (one `YesNo` per candidate, keyed by the skill's display identifier): "if the agent loads this skill now and follows it, will that clearly help it handle the latest request?" with the skill's index-line description. The wording lives in `skill_recall.py` and nowhere else. The question keeps the display spelling it was calibrated on (`ava.skills.<identifier>`); the offered note spells each skill as its loadable path (`ava.skills.web_ai.deep_research`), like the index.
- **Candidates**: `agent/graph/prompt/capabilities.py:recall_candidates`: everything `indexed_skills()` covers (entry skills and sub-skills), described by exactly the text its index line quotes (`_one_line`, at most 300 characters). A skill whose description the index withholds as security-flagged is not a candidate, and neither is one `skills_to_expand_at_start` preloaded. Read and already-offered skills are removed by the hook.

The prompt, the model (`skill_recall_model`, default `jev-1.13.0`) and `skill_recall_threshold` (0.60) were calibrated together by the 2026-10-11 benchmark. They are one unit: change the wording, the description format or the model and the threshold needs recalibrating.

## From scores to a note

Skills whose probability of "yes" reaches the threshold are ranked best first, with the identifier breaking ties (the model's probabilities are coarse, so ties are common), and the top `skill_recall_max` (2) are named in one note in the index's own line shape, with the framing "load with `ava.help(ava.skills.<path>)`". Nothing at or over the threshold means no note.

## The per-window record

A skill is not offered if its body is already in the context window or it was already offered. Two core channels hold that, both cleared by a compaction:

- `skill_reads` (`operator.add` list): the exec child appends the identifier of every skill body it hands over. The one point all body reads pass through is `ava/skills/__init__.py:_record_skill_invoked` (help render, `__doc__`, and `ava.files.read` of a SKILL.md all end there), which also notes it in `ava.state_update["skill_reads"]`. Plain strings only: the child imports no state class, no model stack and no network client for this, and `skill_reads` is in `SDK_WRITTEN_BASE_FIELDS` so the exec node accepts the key.
- `skill_recall` (`SkillRecallState`: `prompted` and `window`, last-value, written only by the hook): the skills a recall note named, and the `compact.version` they belong to.

When `compact.version` is past `window` the hook clears both (`Overwrite([])` and a fresh record) on the first pass after the compaction, whether or not it fires: an exec can only follow an LLM call, which this hook precedes, so no read of the new window is lost to the reset. This is the lazy re-arm the SDK reminder uses.

## Failure semantics

The hook runs before every inbound turn's LLM call, so nothing it does may kill the turn. The whole pass runs under `skill_recall_deadline_seconds` (3.0; independent of `memory_recall_deadline_seconds`); a timeout, any `DecisionError` (provider failure, missing key, unknown model) and a `DecisionInputTooLargeError` are logged under `event=skill_recall` and the turn proceeds with no note. The request grows with the catalog (about 150 tokens per question), so a catalog of roughly 400 skills reaches Jev's 64K request limit; `decide()` then raises `DecisionInputTooLargeError` before sending anything. The state is never truncated and the candidate list is never chunked or sampled: a partial score would offer the best of an arbitrary subset. Such an agent gets no recall until its catalog shrinks or the limit grows, and the warning says so.

## Configuration

`AgentSkillRecallSettings` (`base/config/domains/agent/skill_recall.py`, the `SkillRecall` slice in `base/host/env/agent_slices.py`, all per-agent and live): `skill_recall_enabled` (on), `skill_recall_model`, `skill_recall_threshold`, `skill_recall_max`, `skill_recall_deadline_seconds`. An overlay naming `skill_recall_model` is checked against the catalog's **decision** table (`validate_config_overlay(..., decision_models=...)`); a chat model id is rejected.

Parent: [[agent/graph/docs/graph.ava.okf.md|Graph]].
