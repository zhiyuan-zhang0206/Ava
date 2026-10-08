# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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
  - **Enforced by**: `scripts/content_lint/lint_migrations.py --check-reversible` (blocked in CI)
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
