---
name: ava-serious-engineering
description: Guides trustworthy design, implementation, review, and evolution of complex software systems. Use when business complexity, domain modeling, conceptual integrity, or long-term changeability matters, even if the user asks only for code.
---

# Serious Software Engineering

## Mission

Serious software engineering is the discipline of building systems that stay
understandable, changeable, and correct as they grow — when the hard part is
not writing code, but knowing what to write and keeping the system coherent
once it exists. It covers software engineering **and** the domain modeling that
surrounds it: a business often *is* a software system, and most of its
complexity lives in the software. This skill family deliberately stops at that
boundary — marketing, growth, and operations are separate crafts.

## Quick Start

Pick the scenario that matches what you are doing; do the 5 must-dos. Each
item links to the skill that spells it out. The full checklists live in the
individual skills — this is the minimum bar, not the whole bar.

**A. Designing a new system or component**
1. **Write the Design Concept first** (one page: what the system is / is not, the core metaphor, top 3–5 invariants) — `principles/conceptual-integrity`
2. **Name the subdomains and draw the context map** (Core vs Supporting vs Generic; which contexts need an ACL) — `principles/bounded-context`
3. **Identify the critical path and its budget** (latency, rows, memory, tokens per request) — `practices/design` + `practices/performance`
4. **Write the ubiquitous-language glossary** and use those words in code, schema, and docs from day one — `principles/ubiquitous-language`
5. **Define the error model explicitly** (what can fail, what callers must handle, what is defined out of existence) — `principles/error-handling`

**B. Changing existing code (feature, fix, refactor)**
1. **Read the design intent before touching code**: the Design Concept (if written), the module's contract, and AGENTS.md — `ai-era/context-explicitness`
2. **State the concept being changed and check it against the Design Concept** — is the change extending the system or introducing a new idea? — `principles/conceptual-integrity`
3. **Write the failing test that reproduces the bug / pins the new contract first** — `practices/testing`
4. **Find the deep change** (make the change in one place; if it leaks to many call sites, the abstraction is wrong) — `principles/complexity-management` + `principles/dependency-management`
5. **Verify, don't assume**: run the tests, measure the critical path, review the diff for what changed beyond the intent — `ai-era/verification-discipline`

**C. Working with AI-generated code**
1. **Write the tests yourself, before accepting the code** — a test the AI also wrote is a tautology — `practices/testing` + `ai-era/verification-discipline`
2. **Check the generated code against explicit architectural invariants**, not just "does it pass" — `ai-era/judgment-and-trust`
3. **Reject copy/paste**: AI duplicates because it lacks cross-file memory; require the same abstraction discipline as human code — `practices/review` + `principles/complexity-management`
4. **Demand that the AI state its assumptions in code/AGENTS.md** — implicit context is where AI-generated code goes wrong — `ai-era/context-explicitness`
5. **Assume the code is wrong until a machine-checkable check says otherwise** (tests, type checks, lint, load tests) — `ai-era/verification-discipline`

## The Three Layers

| Layer | What it is | Stability |
|---|---|---|
| `principles/` | Enduring principles — complexity, conceptual integrity, ubiquitous language, bounded contexts, dependencies, error handling, supple design | Stable across eras |
| `practices/` | Lifecycle practices — design, implementation, testing, review, maintenance, performance, concurrency, security, observability | Methodology, slowly evolving |
| `ai-era/` | How the AI era changes the application of the above — verification discipline, context explicitness, judgment & trust | Fast-moving; each file carries a dated observation |

The three-layer split exists because **principles are stable while their
application is not** (observation, 2026-08: the field's practical consensus
turns over on the order of months — treat this as an observation, not a
measured law). When in doubt about whether a rule is timeless or current, it
belongs in the layer that matches.

## When to Use

- **Designing a new system or component** → `practices/design` + the relevant principle (usually `complexity-management` first) + Quick Start A
- **The business terms and code disagree** → `principles/ubiquitous-language`
- **A system spans teams, products, or legacy worlds** → `principles/bounded-context`
- **Code is hard to change** → `principles/dependency-management` + `practices/maintenance`
- **A system feels committee-designed — inconsistent APIs, same word meaning different things** → `principles/conceptual-integrity`
- **A design feels clumsy — callers juggle flags, setup, and special cases** → `principles/supple-design`
- **Errors are handled by vibes** → `principles/error-handling`
- **Writing code** (any language, any size) → `practices/implementation` (+ `practices/testing` as you go)
- **Load, latency, or resource budgets are involved** → `practices/performance`
- **Multiple execution flows share state** → `practices/concurrency`
- **Writing or reviewing tests** → `practices/testing` + `practices/review`
- **Handling secrets, input, or trust boundaries** → `practices/security`
- **A system fails and nobody sees why** → `practices/observability`
- **Working with AI-generated code** → `ai-era/verification-discipline` first, then `ai-era/judgment-and-trust` + Quick Start C
- **Setting up a repo/AGENTS.md for agents** → `ai-era/context-explicitness`

## How to Use

1. Read the layer/principle that matches your task (above, or the Quick Start).
2. Each skill has a **checklist** — run it, don't skim it. Items are marked
   `MUST` (non-negotiable) or `SHOULD` (strongly recommended; skip only with a
   stated reason).
3. Each skill cites its **sources** — when a claim matters, go read the source
   (`references/` holds the five-book distillations and complexity classics).
4. For AI-era guidance, check the dated observation and ask whether it still
   holds.

## Concept Map (quick navigation)

```
Complexity (01, Gall) ──► deep modules ──► interfaces ──► dependencies (03)
        │                                        │
        └── conceptual integrity (02) ──► consistency ──► naming (03)
                                                     │
        Ubiquitous language (04/05) ◄── business ────┘
        │
        └── bounded context (04/05) ──► context map ──► ACL
                │
                └── subdomains (Core/Supporting/Generic)
                       │
                       └── supple design (05) ──► refactoring toward intent-revealing code
                                                     │
        Error handling (01 §4, 03) ◄── failure model ─┘
        │
        └── security (03 Tips 72-73) ──► trust boundaries
        └── observability ◄── every failure that escapes the error model
```

The `ai-era/` layer sits on top of the whole map: verification discipline,
context explicitness, and judgment & trust change *how* the above is applied,
not the map itself.

## Sources

- `references/01-philosophy-of-software-design.md` — Ousterhout, *A Philosophy of Software Design*
- `references/02-design-of-design.md` — Brooks, *The Design of Design*
- `references/03-pragmatic-programmer.md` — Thomas & Hunt, *The Pragmatic Programmer*
- `references/04-implementing-ddd.md` — Vernon, *Implementing Domain-Driven Design*
- `references/05-domain-driven-design.md` — Evans, *Domain-Driven Design*
- `references/06-simon-the-sciences-of-the-artificial.md` — Simon, *The Sciences of the Artificial*
- `references/07-weinberg-general-systems-thinking.md` — Weinberg, *An Introduction to General Systems Thinking*
- `references/08-gall-systemantics.md` — Gall, *Systemantics*
- Research base: a private workspace with ~320 findings on the AI-era shift (`synthesis.md` is the entry point)
