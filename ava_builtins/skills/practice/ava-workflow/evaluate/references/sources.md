# Evaluation Method Sources

Checked 2026-10-15. These sources inform the method; they are not runtime
dependencies and do not establish that this Workflow improves human learning.

| Source | Adopted idea | Adaptation or limit |
|---|---|---|
| Anthropic, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | Distinguish outcomes, trajectories, and graders; read transcripts and calibrate model grading | Workflow applies this beyond software agents and does not prescribe one harness |
| AI Evals Course, [evals-skills](https://github.com/ai-evals-course/evals-skills), especially error-discovery and validate-evaluator | Human observations inform failure categories; judge errors require independent labeled evidence | Sampling ratios and accuracy thresholds are contextual, not universal requirements |
| Shankar et al., [Who Validates the Validators? Aligning LLM-Assisted Evaluation of LLM Outputs with Human Preferences](https://arxiv.org/abs/2404.12272) | Criteria can evolve while people inspect concrete outputs | Motivates revisable rubrics; does not prove a general optimal human-attention policy |
| Anthropic, [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) | Compare skill versions with preserved outputs, assertions, and human review | Ava's case/replay mechanics remain in its skill evaluation protocol; selection data is not a final holdout |
| Microsoft, [SkillOpt](https://github.com/microsoft/SkillOpt) and [paper](https://arxiv.org/abs/2605.23904) | Skill optimization needs explicit evaluators, comparisons, and separation of optimization from confirmation | A useful automated objective is a prerequisite, not a solution to discovering human quality criteria |

Three-way Align, decision value per unit of human attention, and transfer checks
for both human and agent understanding are this Workflow's synthesis. They
need evaluation in real use. Curated surprises support discovery; they do not
by themselves demonstrate learning or estimate production reliability.
