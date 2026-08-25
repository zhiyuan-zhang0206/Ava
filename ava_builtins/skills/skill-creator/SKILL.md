---
name: skill-creator
description: Creates, improves, tests, and reviews Ava skills with strong trigger descriptions and concise instructions. Use when creating a skill, editing any `SKILL.md`, auditing descriptions, or deciding how reusable agent guidance should be packaged.
---

# Skill Creator

Methodology and workflow for creating high-quality Ava skills. Distilled from Claude Code's Skill Creator, adapted to Ava's architecture and tool ecosystem.

## What is a skill

A skill is a reusable instruction pack that tells Ava **what to do in a specific scenario**.

Ava's skill format is the [Agent Skills](https://agentskills.io) open standard, so a skill you write here also runs in Claude Code, Codex and every other client that reads `SKILL.md` — and any skill from that ecosystem installs here unmodified with `ava skill install <git-url-or-path>`. Ava honors the standard's required `name` + `description`; its optional fields (`license`, `compatibility`, `metadata`, `allowed-tools`) are preserved but not acted on, so do not rely on `allowed-tools` to gate anything — Ava's single tool is `execute_code`. Field-by-field detail: `okf/skills/skills.ava.okf.md`.

### Anatomy of a skill

```
skill-name/
├── SKILL.md          (required) YAML frontmatter + Markdown body
└── Bundled Resources  (optional)
    ├── scripts/       - executable code (Python/Node/bash)
    ├── references/    - on-demand documentation
    └── assets/        - templates, images, fonts, output assets
```

### Three load levels

| Level | Content | When loaded |
|------|------|---------|
| 1. Metadata | `name` + `description` | Always in context |
| 2. SKILL.md body | Full instructions | When the skill is triggered |
| 3. Resource files | scripts/references/assets | Only when referenced in instructions |

**Description is the only trigger signal.** Write both "what it does" and "when to use it" clearly.

## Creation flow

```
Understand intent → Interview & research → Draft SKILL.md → Test → Evaluate → Iterate → Done
```

### 1. Capture intent

Ask four core questions:
1. What does this skill help Ava accomplish?
2. In what scenarios should it trigger?
3. What output format is expected?
4. Should we write test cases? (Tasks with objective right/wrong answers suit tests; subjective tasks like style/aesthetic usually do not)

### 2. Interview and research

Actively probe edge cases: input/output formats, example files, success criteria, external dependencies. If a usable MCP server or docs Ava can search exist, research first before writing.

### 3. Draft SKILL.md

**Frontmatter:**
```yaml
---
name: skill-name          # kebab-case
description: What it does + when to trigger. Better to err on the "pushy" side than overly subtle — Ava tends to under-trigger (skill applies but isn't used).
---
```

**Tips for writing the description:**
- ❌ "Extract text and tables from PDFs" — too dry, may not trigger
- ✅ "Extract text and tables from PDFs, fill in forms, merge documents. Use this skill when the user mentions PDFs, forms, document processing, or data extraction."

**Body writing principles:**

1. **Imperative voice** — "Read AGENTS.md first." rather than "You should read AGENTS.md."
2. **Explain why** — LLMs work more effectively when they understand intent than when they rote-memorize instructions; explain why each step matters
3. **Keep it concise** — keep SKILL.md under 500 lines; when it's about to exceed, split out into references/
4. **Avoid overfitting** — A skill is for millions of uses; don't keep micro-tuning to a few examples
5. **Define output format** — use templates; when examples are needed, give Input/Output pairs
6. **Spot repetitive work → script it** — helper code Ava repeatedly writes, write it once in `scripts/`
7. **Multi-domain → split via domain** — SKILL.md describes the general flow + selection guide; specific differences go into `references/`

### 4. Test

Write 2–3 test prompts a real user would say. A good prompt has concrete details (filename, column names, company name); a poor prompt has only abstract keywords ("format this data").

### 5. Evaluate and iterate

The core question: **did the skill make Ava's behavior better?**

- Didn't trigger → description not pushy enough
- Triggered but wrong behavior → instructions not clear
- Ava did a lot of unnecessary work → trim redundant instructions
- Mis-trigger into wrong scenario → description too broad

Fix and re-test until satisfied.

## Review checklist

When reviewing an existing skill, go through each item:

**Frontmatter:** `name` is kebab-case? `description` includes "what it does" and "when to use"? Pushy enough?

**Body:** Clear steps/flow? < 500 lines? When referencing bundled resources, instructions on when to read them?

**Instruction quality:** Imperative voice? Explains why? No rigid all-caps warnings? Has output format/examples? Not overfit?

**Bundled Resources:** Is `scripts/` code independently executable? Is `references/` organized on-demand? No useless resources like node_modules/.git?

**Safety:** No malicious code? No misleading descriptions?

**Ava-specific:** Correct use of `ava.shell.run`/`ava.files`/`ava.agents`/`ava.web`? Worktree isolation considered?

## Improving an existing skill

1. **Keep the original name** — directory name and frontmatter `name` unchanged
2. **Read first, change later** — read the entire existing SKILL.md and all references
3. **Identify pain points** — where does it under-trigger? where are instructions unclear? where is it too verbose?
4. **Incremental improvement** — change one issue at a time; don't tear it all down
5. **Compare effects** — run the same test prompts before and after

### Common issues and fixes

| Symptom | Diagnosis | Fix |
|------|------|------|
| Skill doesn't trigger | Description too subtle or too narrow | Add "when to use" context, make it slightly pushy |
| Triggers but behaves randomly | Instructions too vague | Add steps, output format, examples |
| Ava "overthinks" | Instructions verbose or conflicting | Trim redundancy, simplify flow |
| Some scenarios right, others wrong | Missing conditional branch | Add "if X then Y" decision logic |
| Always triggers (mis-trigger) | Description too broad | Narrow the trigger conditions |

## Key differences between Ava and Claude Code

Claude Code has a full eval infrastructure (`run_loop.py`, `generate_review.py`, `.skill` packaging, etc.); Ava does not — the core methodology is the same, but testing differs:

- **Ava's testing:** manually write 2–3 test prompts, run an agent with the skill enabled, human evaluation
- **What Ava does not have:** description auto-optimization, quantitative benchmarks, blind comparison, .skill packaging
- **Ava's edge:** `ava.agents.spawn` can dispatch a sub-agent to test (avoiding the bias of writing-and-testing-yourself); `ava.files` + `ava.shell` have clear tool separation
