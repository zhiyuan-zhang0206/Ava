---
name: ubiquitous-language
description: "Aligns domain vocabulary across code and documentation. Use when resolving synonyms, overloaded terms, or names that obscure business meaning."
---

# Ubiquitous Language

## One-Sentence Core
> A single shared vocabulary — strictly matching every core business term to a code name, a DB table, an API field, and a test fixture — so that the model, the code, and the conversation never drift apart.

## Core Principles

- **Language Is the Contract**: every business term has exactly one definition and exactly one code-level name, enforced across the full delivery pipeline (code, DB, APIs, docs, tests). **Why**: Evans (05 §1.3) observed that translation loss between business and code is the single largest source of hidden bugs — when a domain expert says "ticket" and the code says `Task` while the PM doc says "work item," every meeting requires mental translation, and the gap between what is said and what is built accumulates silently. Vernon (04 §2.1) widened this: the language must cover the full pipeline, not just class names, because a drift in any one artifact — a DB column, an API response field, a test fixture name — reopens the translation gap. **How**: before coding any new concept, write its definition in a shared glossary (Thomas & Hunt Tip 80, 03 §9); validate that the class name, DB column, API field, and test fixture all use the same term; run a naming audit on every pull request — reject any PR that introduces a synonym or reuses an existing term for a different concept.

- **One Word, One Concept; One Concept, One Word**: when the same word means different things in different parts of the business, split them into distinct named concepts; when two words mean the same thing, collapse them into one. **Why**: Evans (05 §1.3) identified the "task" trap — a word that means "user requirement" in one breath and "internal execution step" in the next — as the clearest signal that a concept must be split (`UserRequest` vs `ExecutionStep`). Conversely, having `User`, `Customer`, `Account`, and `Member` in the same codebase with nobody able to articulate the difference (Vernon 04 §2.1) is a naming debt that compounds with every new hire. **How**: conduct a noun inventory of the codebase every quarter — list every domain noun and ask "does this word mean exactly one thing everywhere it appears?" If not, split it; if two nouns overlap, merge them. Thomas & Hunt Tip 74 (03 §8.5) adds the practical trigger: when a name stops fitting, rename it immediately — do not let stale names accumulate.

- **The Language Owns the Model; the Model Drives the Code**: the ubiquitous language is not a documentation artifact bolted on after design — it is the design, and the code is its executable projection. **Why**: Evans' Model-Driven Design (05 §1.4) demands bidirectional binding: a change in the language must force a code change, and an awkward abstraction in code signals that the model itself is wrong. When the language is treated as an afterthought, the model becomes whatever the code happens to do — and the code drifts from what the business actually needs. **How**: every domain-concept discussion (refinement, Knowledge Crunching session) must end with synchronized code renames; an "analysis model" that is not reflected in the code is waste (05 §1.4). Make class names directly understandable and correctable by domain experts — if a domain expert cannot read the class name and say "yes, that is what we call it," the language is broken.

- **Ambiguity Triggers Immediate Alignment**: any moment a term's meaning is ambiguous — in a meeting, a spec, a code review — stop and resolve it before proceeding. **Why**: unresolved ambiguity is the root of change amplification (Ousterhout 01 §1.2): a term that means different things to different people creates dependencies nobody sees until they break. Evans (05 §1.3) prescribes immediate alignment meetings with synchronized renames as the only response. **How**: flag ambiguous terms in code review with a "language drift" label; maintain a living glossary (Thomas & Hunt Tip 80, 03 §9) checked into version control alongside the code; every glossary entry carries the term, its single definition, where it applies (which Bounded Context), and what it must not be confused with.

## Checklist
- [ ] **MUST** Does every core business noun have exactly one class/type/table/field name in the codebase?
- [ ] **SHOULD** Can a domain expert read the class names and confirm they match the business vocabulary?
- [ ] **MUST** Are there any synonyms in the codebase — two different names for the same concept?
- [ ] **MUST** Are there any homonyms — the same name used for different concepts in different contexts?
- [ ] **SHOULD** Does the glossary live in version control and is it updated with every PR that introduces or renames a concept?
- [ ] **MUST** Do DB column names, API field names, and test fixture names match the ubiquitous language names?
- [ ] **MUST** Is every ambiguous term flagged and resolved before the PR merges?
- [ ] **SHOULD** Do method names state intent in business terms rather than implementation details?

## Relationships
- **principles/bounded-context**: a Ubiquitous Language is always scoped to one Bounded Context; the same word across contexts means different things — that is the context boundary signal.
- **principles/dependency-management**: naming consistency reduces hidden dependencies; a renamed concept that breaks downstream code reveals a coupling that should have been explicit.
- **practices/design**: the language is the primary output of the modeling phase; the model is the language made structural.
- **references/05-domain-driven-design.md §1.3**: Evans' original formulation — language anchored in code names, bidirectional binding to the model.
- **references/04-implementing-ddd.md §2.1**: Vernon's extension to the full delivery pipeline — DB, APIs, docs, tests.
- **references/03-pragmatic-programmer.md**: Tip 74 (naming as signal), Tip 80 (project glossary).
- **references/01-philosophy-of-software-design.md §1.2**: complexity symptom "cognitive load" — inconsistent naming is a primary contributor.

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
