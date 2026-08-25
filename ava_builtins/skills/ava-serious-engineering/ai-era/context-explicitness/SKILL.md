---
name: context-explicitness
description: Makes repository assumptions, constraints, boundaries, and agent context explicit and self-verifying. Use when setting up AI-agent repos, editing `AGENTS.md` or `CLAUDE.md`, designing module boundaries, or diagnosing recurring agent mistakes.
---

# Context Explicitness

## One-Sentence Core
> In the AI era, the repository is the agent's memory — every assumption, constraint, and invariant must be explicit, readable by machines, and backed by a mechanism that turns red when it becomes false.

## Core Principles

- **Repository as Memory**: Agents have no long-term memory between sessions — the codebase must carry every non-obvious fact they need to work correctly. — **Why**: Martin Fowler (ThoughtWorks, 2026): file I/O and search are "the most fundamental context interfaces" — the quality of the codebase as a knowledge store directly determines agent effectiveness. Anthropic's Context Engineering guide (2026-04) frames context as the meta-discipline: "tool design, sub-agent architecture, and compaction all serve one goal — getting the right information into the right window at the right time." The Codified Context study (arXiv 2602.20478) demonstrated zero repeat errors across 74 sessions when context infrastructure was structured, versus unmeasured error rates without it. The X community consensus by mid-2026: "Memory is just better context engineering" (119 likes, 2026-04). — **How**: Every architectural decision, constraint, and invariant that a new team member would learn from a senior engineer must be written down in version-controlled context files (AGENTS.md, .cursor/rules, module-level READMEs). The test is: can a fresh agent, reading only the repository, avoid the same mistake twice? If not, the repository's memory is incomplete.

- **Self-Describing Code That Turns Red When Wrong**: Every claim in the repository must have a mechanism that fails visibly when the claim becomes false — prose without enforcement is a liability. — **Why**: The Context Architecture principle (context-architecture.dev, 2026): "a readme without a mechanism that turns red when it lies is just prose — it rots invisibly." The community's highest-leverage single practice (45 likes, 2026-06): "When corrected, propose an edit to AGENTS.md so the same mistake doesn't recur." This feedback loop — mistake → fix → update context rule → rule enforced → mistake impossible — is the mechanism by which a repository learns. Without enforcement, rules decay; with enforcement, they compound. — **How**: Pair every "must" or "never" statement in AGENTS.md with an automated check (linter rule, CI step, architecture test, or compilation error). The gold standard: an AGENTS.md rule whose violation fails CI. The silver standard: a rule whose violation triggers a warning. The minimum viable: a rule checked during code review with a written checklist. Prose-only rules are treated as absent.

- **Modularity Is Now a Hard Constraint**: What was once "good practice" — clean module boundaries — is now a survival requirement, because agents operate within bounded context windows that degrade with noise. — **Why**: Harvard's "The Modular Imperative" (LMPL '25, namin.seas.harvard.edu) demonstrated that LLMs suffer a ~40% accuracy drop when architectural boundaries are blurred — agents cannot reason reliably across tangled dependencies. Martin Fowler codified this as "AI-friendly codebase design": code that serves as its own context requires locality, clear contracts, and minimal cross-cutting sprawl. Factory.ai (2026): even 1M-token context windows cannot bridge the structural gap to million-line enterprise codebases — the solution is not bigger windows but better modularity. GitClear's 5-year study (2.11 billion lines, 2020-2024) found AI tools drove a 4× increase in copy/paste code and a 60% decline in refactoring (code movement) — the exact opposite of the modularity the era demands. — **How**: Enforce module boundaries with tools, not conventions — dependency cruisers (e.g., `dependency-cruiser` for JS/TS, `import-linter` for Python), architecture tests that fail on boundary violations, and CI checks that prevent cross-module imports. Every module must be understandable within a single context window (~200K tokens as of 2026-08); if it cannot, it must be split. Design module interfaces as API contracts — explicit, versioned, and tested.

- **1M Token Windows Do Not End Context Management**: Larger windows introduce "context rot" — irrelevant information actively degrades reasoning quality, and token costs scale exponentially. — **Why**: Factory.ai's analysis (2026): "context decay" means that even when the window is large enough, too much irrelevant information reduces reasoning quality — the model loses signal in noise. Anthropic's own guidance: "Tool design should target the minimum viable tool set" — fewer, clearer tools beat many overlapping ones because ambiguity degrades decisions. The X community in 2026H1 shifted from celebrating 1M-token models (Gemini 3.1 Pro, Claude Mythos, GPT-5.4, DeepSeek V4 Pro, GLM 5.2 — all shipping 1M+ by mid-2026) to asking "how do we manage such large contexts without losing reasoning quality?" The consensus (timeline-synthesis, 2026-08): bigger windows raised the ceiling but did not solve the problem — context engineering, not window size, is the limiting factor. — **How**: Practice minimum-sufficient-context: give the agent exactly what it needs, nothing more. Use sub-agents for task decomposition — each sub-agent operates in a clean, bounded context and returns a condensed summary (Anthropic recommends 1,000–2,000 token summaries from sub-agents). Treat context budget as a first-class resource — track what goes into the window, measure its effectiveness, and optimize continuously (Context Engineering 2.0 principle: context is a process, not a one-time design).

- **Explicitness Has a Measurable Cost — Budget It**: Maintaining AI-readable context infrastructure costs roughly 24% of the codebase size — this is an investment, not overhead, but it must be budgeted and maintained. — **Why**: The Codified Context study (arXiv 2602.20478) measured a 24.2% "knowledge-to-code ratio": 26,000 lines of context infrastructure (project constitution, 19 domain-agent specs, 34 knowledge-base documents) supporting 108,000 lines of production code. This is the empirical cost of making a codebase agent-compatible — significant enough that it must be treated as a first-class engineering activity, not an afterthought. The payoff: zero repeat errors across 74 sessions. Without maintenance, context infrastructure rots — stale AGENTS.md rules are worse than no rules because they actively mislead. — **How**: Budget context maintenance in sprint planning — updating AGENTS.md is part of "done," not an extra. Prioritize high-leverage context: frequently encountered errors, architectural invariants, and module contracts. De-prioritize exhaustive documentation of stable, self-evident code. Run periodic "context audits": load the repository into a fresh agent and measure how many mistakes it makes before producing correct work — the error rate is the context quality metric.

- **Fix the Rule, Not Just the Bug**: When a mistake happens, the fix is incomplete until the context rule is updated to prevent recurrence — treating symptoms without updating the immune system guarantees reinfection. — **Why**: The highest-leverage single practice identified in the 2026H1 X community (45 likes, 2026-06): "The best rule I stole from @zeke: when corrected, propose an edit to AGENTS.md so the same mistake doesn't recur." This turns every bug into a permanent learning event for every future agent. Tencent's Team Memory initiative (chrome-synthesis, 2026-08) and Uber's 65-72% AI-generated code with accompanying governance (2026H1) both operationalize this at scale: the rulebase, not the codebase, is the organization's learning system. Ava's own memory architecture (shared pool + per-agent memory) is a direct implementation of this principle. — **How**: Every bug fix or code review correction that reveals a missing or incorrect constraint must include an AGENTS.md update before the PR merges. The update is blocking — not optional, not a follow-up ticket. The test for completeness: "If another agent encounters this same situation tomorrow, will it make the same mistake or will the context rule prevent it?"

## Checklist

- [ ] **MUST** Does every non-obvious architectural constraint have a machine-enforceable rule (linter, test, CI check, or compilation error) — or is it prose-only?
- [ ] **MUST** Can a fresh agent understand this module's boundaries, contracts, and invariants by reading only the context files adjacent to it (AGENTS.md, module README)?
- [ ] **MUST** Is the AGENTS.md (or equivalent) updated in the same PR as every correction — not deferred to a follow-up?
- [ ] **SHOULD** Do module boundaries survive the context-window test: can an agent understand the module without loading the entire codebase into its window?
- [ ] **MUST** Is there a mechanism that "turns red" when a stated invariant becomes false — or does the invariant rely on human vigilance?
- [ ] **SHOULD** Is the context infrastructure (AGENTS.md, rules, module docs) budgeted as a first-class maintenance activity — or is it left to rot between major refactors?
- [ ] **SHOULD** Are context files concise enough to be fully loaded when relevant, rather than so large that agents skip them?
- [ ] **SHOULD** When was the last context audit — loading a fresh agent and measuring its error rate before it self-corrects?

## Anti-Patterns

- **Prose-Only Rules**: AGENTS.md rules written in natural language with no automated enforcement — they rot silently. → **alternative**: Every "must" or "never" statement is paired with at minimum a review-checklist item, ideally a CI-enforced check.
- **Context Hoarding**: Dumping every document, spec, and wiki page into the context window "just in case." → **alternative**: Minimum-sufficient-context — let the agent search and grep for what it needs; provide tiered context (project-level → module-level → inline).
- **Stale AGENTS.md**: Rules that no longer apply but were never removed — they actively mislead agents. → **alternative**: Treat AGENTS.md staleness as a bug of equal severity to a failing test; review on every significant change; prune obsolete rules aggressively.
- **Monolithic Context Blob**: One massive README or AGENTS.md trying to explain everything in the repository. → **alternative**: Tiered context architecture (Codified Context model): project constitution (always loaded, <1K lines), domain-agent specs (loaded by task relevance), knowledge-base documents (on-demand via search).
- **Single-Point-of-Failure Context**: All context knowledge lives in one person's head or one Slack channel. → **alternative**: Every important discovery during development is written into the repository before the session ends — "if it's not in the repo, it doesn't exist."
- **Bigger-Window Fallacy**: Assuming that because models now support 1M-token windows, context management is no longer necessary. → **alternative**: Recognize that context quality, not quantity, limits agent performance; bigger windows make good context engineering more important, not less.

## Examples

### 1. Prose-only rule vs. enforced rule

```markdown
<!-- BAD: AGENTS.md rule with no enforcement — rots silently -->
## Database Rules
- Never use raw SQL queries — always use the ORM.
- All migrations must be reversible.

<!-- Six months later: raw SQL is everywhere, nobody noticed. -->

<!-- GOOD: AGENTS.md rule paired with enforcement -->
## Database Rules
- Never use raw SQL queries — always use the ORM.
  - **Enforced by**: `scripts/lint_raw_sql.sh` (blocked in CI)
  - **Exception process**: Add table+column to `.allowed-raw-sql.json` with justification
- All migrations must be reversible.
  - **Enforced by**: `scripts/lint_migrations.py --check-reversible` (blocked in CI)
```

### 2. Module boundary without vs. with structural enforcement

```python
# BAD: Module boundary exists only in documentation
# architecture.md says: "auth/ never imports from billing/"
# But nothing enforces it. An agent writes:
# auth/login.py:
from billing.invoice import generate_invoice  # Crosses boundary — no error.

# GOOD: Module boundary enforced by tooling
# .importlinter:
# [contracts]
# auth_independent_of_billing =
#   name = "Auth module does not depend on Billing"
#   type = "independence"
#   modules = ["auth"]
#   independent_of = ["billing"]
#
# CI runs: `import-linter` → FAILS, blocks merge
# The boundary is real because it has teeth.
```

### 3. Context file left to rot vs. maintained as first-class artifact

```
# BAD: AGENTS.md last updated 2025-09, three major refactors ago
$ git log --oneline -- AGENTS.md
a1b2c3d (2025-09-12) Initial AGENTS.md

# Agents make the same mistakes repeatedly; nobody connects it to stale rules.

# GOOD: AGENTS.md evolves with every correction
$ git log --oneline -- AGENTS.md
e5f6g7h (2026-08-05) Rule: never use .get() on required config — PanPan incident
d8e9f0a (2026-08-02) Rule: Redis connections must set socket_timeout — #1234
b1c2d3e (2026-07-28) Rule: migrations require .down.sql — CI now enforces

# The rulebase is the team's immune system, updated at the point of learning.
```

## Relationships

- **`principles/complexity-management/SKILL.md`** — the timeless principle that complexity must be managed; context explicitness is how that principle is operationalized for agents.
- **`principles/bounded-context/SKILL.md`** — bounded contexts from DDD are the conceptual foundation for module boundaries that agents can navigate.
- **`principles/conceptual-integrity/SKILL.md`** — conceptual integrity (Brooks) is what context files must capture; without it, context files are just disorganized notes.
- **`ai-era/verification-discipline/SKILL.md`** — explicit context provides the contracts that verification discipline verifies against; the two skills are complementary halves of the "trust but verify" loop.
- **`ai-era/judgment-and-trust/SKILL.md`** — context explicitness helps agents make better judgments by giving them explicit criteria; judgment-and-trust covers when to override those criteria.
- **`ai-era/guidance.md`** — the overarching map of which timeless principles are upgraded, which are unchanged, and which are made obsolete by the AI era.

## Sources

- **Martin Fowler — Context Engineering for Coding Agents** (ThoughtWorks, 2026) — file I/O and search as foundational context interfaces; AI-friendly codebase design (`research/ai-era-software-engineering.md` §2.1)
- **Anthropic — Effective Context Engineering for AI Agents** (2026-04) — minimum viable tool set; sub-agent architecture; context as meta-discipline (`research/ai-era-software-engineering.md` §2.2)
- **Codified Context — arXiv 2602.20478** (2026) — 24.2% knowledge-to-code ratio; three-tier context architecture; zero repeat errors (`research/ai-era-software-engineering.md` §2.3)
- **Context Architecture (context-architecture.dev)** — "every readme must have a mechanism that turns red when it lies" (`research/ai-era-software-engineering.md` §2.4)
- **Harvard — The Modular Imperative (LMPL '25)** — 40% accuracy drop on blurred boundaries; modularity as hard constraint (`research/ai-era-software-engineering.md` §2.6)
- **Factory.ai — The Context Window Problem** (2026) — context decay; structural gap between window size and codebase size (`research/ai-era-software-engineering.md` §2.5)
- **GitClear — AI Copilot Code Quality: 2025 Data** — copy/pasted lines 8.3% → 12.3% (2020→2024, +48% relative; cloning rate 4× year-over-year); moved-code share fell 25% → <10% (≈60% relative decline) — <https://www.gitclear.com/ai_assistant_code_quality_2025_research> (`research/ai-era-software-engineering.md` §1.5)
- **"When corrected, propose an edit to AGENTS.md"** — community highest-leverage practice (45 likes, 2026-06) (`research/x-search/context-2026H1.md` §8)
- **"Memory is just better context engineering"** (119 likes, 2026-04) — reframing memory as context engineering's highest-leverage form (`research/x-search/context-2026H1.md` §3)
- **AI Engineer World's Fair 2026** — Context Engineering as dominant theme (6,000+ attendees) (`research/x-search/context-2026H1.md` §2)
- **Tencent Team Memory & Uber 65-72% AI governance** — enterprise-scale context infrastructure (`research/x-search/chrome-synthesis.md` §2)
- **Context Engineering 2.0 (GAIR, 2025-11)** — context as process, not one-time design (`research/x-search/context-2025H2.md` §8-9)
- **Observation date**: 2026-08 — the 1M-token window became standard in mid-2026, but the field's consensus is that context engineering, not window size, is the binding constraint; re-evaluate as models evolve.
