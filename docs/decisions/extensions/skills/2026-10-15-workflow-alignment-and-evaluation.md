# Workflow aligns understanding and evaluates evidence

## Context

Separate Calibrate and Align guides made calibration look like a preliminary
phase, while Align concentrated on extracting intent. Work & Evaluate focused
on checking execution against established criteria. This left too little
guidance for learning the criteria, correcting human and agent beliefs, and
using limited human attention well.

## Decision

Align owns three relationships: agent understanding of human intent, human
understanding of reality, and agent understanding of reality. Calibration is
part of that ongoing responsibility. Human preferences and authority remain
distinct from factual claims that either participant can get wrong.

Evaluate replaces Work & Evaluate as an independently usable Workflow guide.
It owns general evidence selection, trace review, human review, judge
calibration, controlled comparison, and feedback. Engineering and research
reference it and retain their domain methods.

Workflow also owns tracer bullets as a general principle: exercise a small
real path through outcome and feedback before expanding an untested approach.
The software pattern motivates this extension; its use beyond engineering
is an explicit design choice.

Capability–Timescale is promoted to a first-class working dimension. Current
capability can change which goals are feasible, which approach to choose, and
what resources or schedule are defensible. Its evidence feeds Align, Define
Goal, Plan, and Evaluate; it is not subordinate to planning or a one-time
estimate. The existing skill identity and system-prompt route remain valid.
Define Goal supports anchored qualitative and learning outcomes, while Plan
supports small serial dependencies and omits unsupported or unrequested times.

## Consequences

There is one owner for shared alignment and evaluation methods. The guides
can interleave without mandatory stages, repeated permission checks, or
evaluation infrastructure for simple work. Targeted surprise samples help
discover problems; separate sampling supports population claims.

The general guide requires domain-specific criteria and evidence. Its aim of
improving understanding per unit of human attention still needs real human
review and transfer evidence; instruction-following cases cannot establish it.
The old Calibrate and Work & Evaluate entrypoints are removed, and current
references move to Align and Evaluate.

## Alternatives

Keeping Calibrate as a separate stage would preserve a distinction in names
but obscure that both participants need continuous calibration. Placing
Evaluate under serious engineering would make other domains depend on an
engineering-specific entry for a general method. A fixed evaluation pipeline
would add unnecessary process to tasks that need only a direct check.
Leaving capability calibration as an incidental planning reference would keep
stale capability assumptions upstream of goal and strategy selection.

Current guidance lives in [Workflow](../../../../ava_builtins/skills/practice/ava-workflow/SKILL.md).
