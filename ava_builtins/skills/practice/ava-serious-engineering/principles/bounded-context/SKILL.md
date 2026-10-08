---
name: bounded-context
description: "Separates domain models and language into explicit contexts. Use when defining service boundaries or integrating models with incompatible meanings."
---

# Bounded Context

## One-Sentence Core
> A Bounded Context is an independent, self-consistent language world — every word inside has exactly one meaning, and across the boundary the same word may mean something entirely different; the whole art of strategic design is drawing these boundaries well and defining clean relationships between them.

## Core Principles

- **A Model Is Valid Only Inside Its Context**: the same noun — "Customer," "Product," "Order" — carries a completely different model, different attributes, different behavior, and different invariants in each Bounded Context where it appears. **Why**: Evans (05 §4.1) showed that a unified `Customer` class across sales, logistics, and support inevitably grows to 200+ fields that nobody dares touch — each context needs only a subset of attributes, and the attempt to satisfy all of them in one model creates a rigid, incomprehensible monolith. Vernon (04 §2.2) added the implementation signal: when two teams use the same word with different meanings, or two parts of the system mutate the same rows with different invariants, a context boundary is needed. **How**: for every core noun in the system, ask "does this word mean the same thing everywhere it appears?" If the answer is no, split the noun into separate context-scoped models, linked by ID and synchronized through events when necessary — never through a shared mutable object.

- **Classify Every Subdomain Before You Build**: the business decomposes into three kinds of territory — Core Domain (your competitive advantage, deserves the best people and deepest modeling), Supporting Subdomain (needed but not differentiating, good-enough is fine), and Generic Subdomain (everyone needs it, buy or use open source). **Why**: Evans (05 §4.3, "Distillation") and Vernon (04 §2.3, "Subdomain Classification") both observed that teams routinely spend 80% of their effort on generic subdomains — auth, logging, notification plumbing — while rushing the core domain where the business actually differentiates. This is an investment error, not a technical one: strategic thinking must come first. **How**: before writing code for a new feature, classify which subdomain it belongs to; for Generic subdomains, default to off-the-shelf solutions; for Core Domain, allocate the most rigorous modeling (Aggregates, Domain Events, Ubiquitous Language refinement) and protect it from generic work.

- **Context Map Every Relationship**: every pair of Bounded Contexts that interact must have an explicit, named relationship — Partnership, Customer-Supplier, Conformist, Anticorruption Layer, Open Host Service, Published Language, Shared Kernel, or Separate Ways. **Why**: unnamed, implicit relationships between contexts accumulate hidden coupling — one team changes their model and another team's system breaks silently because nobody documented the dependency. Vernon's canonical eight-relationship map (04 §2.4) makes every integration decision explicit and negotiable. **How**: draw a Context Map for the system and keep it current; for every cross-context integration, pick and name the relationship type in the architecture decision record; the map must be visible to every team so that integration costs are never a surprise.

- **Anticorruption Layers Are Non-Negotiable for External Systems**: any integration with a system whose model you do not control — a third-party API, a legacy system, a partner's service — must pass through a translation layer that maps their concepts into yours before they touch your domain logic. **Why**: Evans (05 §4.2) named the Anticorruption Layer as the single most important defensive pattern in strategic design; external systems iterate on their own schedule, their models are optimized for their own concerns, and without a translation barrier their design decisions leak into your core and become permanent constraints. Vernon (04 §2.4) reinforced this: the ACL is the difference between "we integrate with Salesforce" and "Salesforce's data model now dictates our architecture." **How**: build a dedicated adapter module for each external system; its sole job is to translate external DTOs/events into your domain's Ubiquitous Language objects before any domain logic sees them; test the translation in isolation so that when the external API changes, only the ACL module breaks.

- **Boundaries Are Verified by Friction**: the most reliable signal that a Bounded Context boundary is wrong is friction — two models that are constantly changed together, or a cross-context operation that requires a distributed transaction when it should be a local one. **Why**: a boundary that forces a distributed transaction for every update is a boundary drawn in the wrong place; the Aggregate rule (Vernon 04 §4.6) says one transaction modifies one Aggregate — if you consistently need atomic writes across two "contexts," they may be one context, or the boundary is slicing the wrong seam. **How**: track cross-context change frequency; if two contexts are always modified in the same PR, question the boundary; if a cross-context operation always needs a compensating saga, verify that eventual consistency is actually acceptable at that seam.

## Checklist
- [ ] **MUST** Does every domain noun have a single, unambiguous meaning within each Bounded Context?
- [ ] **MUST** Are contexts that use the same word for different concepts explicitly separated (different namespaces, different modules, different services)?
- [ ] **SHOULD** Is every subdomain classified as Core, Supporting, or Generic — and is investment proportional to the classification?
- [ ] **MUST** Does a Context Map document every relationship between contexts, with an explicit relationship type?
- [ ] **MUST** Is every external system (third-party API, legacy system) protected by an Anticorruption Layer?
- [ ] **MUST** Are cross-context operations that require atomic consistency identified and handled (either merged into one context or made eventually consistent with a saga)?
- [ ] **SHOULD** Do teams own their contexts, or do multiple teams share ownership of one context?
- [ ] **SHOULD** Can one context be deployed and tested independently of all others?

## Relationships
- **principles/ubiquitous-language**: each Bounded Context has its own Ubiquitous Language; the language boundary defines the context boundary.
- **principles/dependency-management**: the Context Map relationship types (Customer-Supplier, Conformist, ACL) are dependency-management decisions scaled to the system level.
- **principles/supple-design**: inside each Bounded Context, supple design patterns (intention-revealing interfaces, side-effect-free functions) keep the model expressive and changeable.
- **practices/design**: strategic design (context boundaries, subdomain classification) precedes tactical design (entities, aggregates, services).
- **references/05-domain-driven-design.md §4.1–4.3**: Evans' original formulation of Bounded Context, Context Map, and Distillation.
- **references/04-implementing-ddd.md §2.2–2.4**: Vernon's implementation treatment — subdomain classification table, canonical relationship list, signals that a boundary is needed.

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
