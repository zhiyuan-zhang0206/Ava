---
name: design
description: "Designs software components and significant features. Use when clarifying interfaces, user models, tradeoffs, or architectural decisions before implementation."
---

# Design Practice

## One-Sentence Core
> Good design is the deliberate act of understanding the problem, making implicit assumptions explicit, exploring alternatives, and recording *why* — before committing to *what*.

## Core Principles

- **Prior Art and the Simplest Sufficient Design First**: before designing a subsystem, write down how far the problem must be solved at the system's real scale, how established tools and practices already solve it, and the simplest design that would suffice; every mechanism beyond that design must name the failure it prevents. **Why**: the unified cluster lifecycle was sized to an idealized threat model. It re-derived release directories, orchestration, schema-version checks, and stale-writer protection from first principles, and took 274 commits and eight review rounds to land. Its release path then could not run on the only topology the project uses, and a stop, checkout, sync, and start script replaced it ([`docs/postmortems/0009`](../../../../../../docs/postmortems/0009-complexity-must-name-the-failure-it-prevents.md)). **How**: open the design doc with three sections, in this order:
  1. **Scope**: the failures to prevent and the scale assumptions.
  2. **Prior art**: named tools or patterns, and what each does.
  3. **Simplest sufficient design**: the design, and the specific failure it cannot handle.

  Justify each added mechanism by a failure that has happened, or a credible one that cannot be handled by hand when it happens. Build the simplest path end to end on the real topology before any breadth.

- **Design It Twice**: for every non-trivial design, conceive at least two substantially different alternatives before choosing one — **Why**: the brain's first instinct is usually the most familiar solution, not the best; comparing alternatives surfaces hidden trade-offs and prevents anchoring on a single path (Ousterhout, §4.4). — **How**: before writing implementation code, sketch two distinct approaches on a whiteboard or in a design doc; they must differ in at least one structural dimension (e.g. data model, module boundary, sync vs. async); if you cannot produce a second alternative, you do not understand the problem well enough.

- **Explicit User Models — Better Wrong than Vague**: articulate who the users are, what they know, and what they need — as a written model, not an unspoken assumption — **Why**: every team member carries a different implicit user; an articulated guess, however imperfect, exposes disagreement and can be corrected; an unspoken assumption hides until the system is built (Brooks, §3.2). — **How**: write a short user model (1–2 paragraphs + a bullet list of capabilities/constraints) before designing the interface; name the user roles explicitly; review it with at least one stakeholder or real user.

- **Constraints Are Scaffolding**: identify and classify every constraint before designing freely — **Why**: constraints shrink the search space and liberate creativity; but confusing a true constraint with an imagined one ("it must use the existing DB schema") leads to over-constrained designs or wasted effort fighting a phantom (Brooks, §3.4, Thomas & Hunt Tip 81). — **How**: list every constraint and tag each as **true** (laws of physics, regulatory, hard budget), **obsolete** (once true, now gone), **imagined** (habit or fear, the nine-dots problem), or **deliberate** (self-imposed to simplify). Challenge every constraint not tagged "true."

- **Pull Complexity Downward**: when complexity is unavoidable, let the module author bear the pain so every caller stays simple — **Why**: a module has one author but many callers; a complex interface pushes the cost onto every caller forever, while a complex implementation is paid once (Ousterhout, §4.1). — **How**: during design review, count the number of things a caller must know to use each interface; if the answer exceeds 3–4, redesign the interface to hide more complexity behind it (the "deep module" test).

- **Write the Interface Comment First**: before writing a class or function body, write its interface comment — **Why**: if you cannot write a short, clear interface comment, the abstraction is flawed; writing the comment first forces strategic thinking and serves as a design evaluation tool — a comment that must enumerate many special conditions signals over-complexity (Ousterhout, §5.2). — **How**: for every new module, write the interface comment (what it promises, its preconditions, its postconditions) before the implementation; if the comment exceeds 6–8 lines or lists many edge cases, redesign the module.

- **Record the Rationale — Not Just the Decision**: maintain a written record of *why* each significant design decision was made, not only what was chosen — **Why**: when requirements shift or underlying assumptions die, only the rationale tells you whether the decision still holds; without it, every subsequent engineer must reverse-engineer intent from code (Brooks, §3.9; ADR practice from 45ck/software-architecture-skills). — **How**: write an Architecture Decision Record (ADR) for every decision that is hard to reverse, affects multiple modules, or carries a non-obvious trade-off. An ADR has four sections: Context (what problem we faced), Decision (what we chose), Consequences (what becomes easier and harder), and Alternatives Considered (what we rejected and why).
- **General-Purpose Hooks on the Core Path Are a Trap**: idempotency, caching, and retry hooks that intercept every call to a core operation must define their default/missing-value semantics explicitly — a hook that silently swallows work on an absent key is worse than no hook. — **Why**: Layer-1 behavioral eval (2026-08-06, t4): a general idempotency hook keyed on `external_id` matched `None` for every order without that key, so the second order was silently dropped without persisting — silent data loss in the core path. Isolating the hook at an explicit retry entry point (where the caller has opted in) eliminated the class of bug. — **How**: A hook that changes core-path behavior must answer: what happens when the key is absent / null / malformed? Is the hook at an opt-in entry (explicit retry), not woven into the default path? Does a test pin the missing-key semantics?

## Checklist

- [ ] **MUST** Does the design state how far the problem must be solved at real scale, the prior art by name, and the simplest sufficient design? Does every further mechanism name the failure it prevents?
- [ ] **SHOULD** Is there a written user model — who are the users, what do they know, what do they need?
- [ ] **SHOULD** Were at least two substantially different design alternatives considered and compared?
- [ ] **MUST** Are all constraints identified and classified (true / obsolete / imagined / deliberate)?
- [ ] **SHOULD** For each module interface: can a caller understand it without reading the implementation?
- [ ] **SHOULD** Does each interface comment fit in 6–8 lines without enumerating edge cases?
- [ ] **MUST** Is every design decision that is hard to reverse, cross-cutting, or non-obvious recorded as an ADR?
- [ ] **MUST** Does the ADR include the alternatives that were rejected and why?
- [ ] **MUST** Are scarce resources (budget, latency budget, DB connections, human attention) explicitly budgeted in the design?
- [ ] **MUST** If a general-purpose hook (idempotency, caching, retry) intercepts the core path: are its default/missing-value semantics (absent key, null key, malformed key) explicitly defined AND pinned by a test? Is it isolated at an opt-in entry rather than woven into the default path?

## Relationships

- **Principles**: `principles/complexity-management` — the deep-module test and pull-complexity-down are the design-phase application of complexity management; `principles/conceptual-integrity` — the design concept and chief-architect model from Brooks are the organizational side of what "design it twice" and ADRs achieve technically.
- **Practices**: `practices/implementation` — the interface comment written during design becomes the contract implementation must fulfill; `practices/review` — ADRs are primary input to design review.
- **References**: `../../references/01-philosophy-of-software-design.md` §4.1, §4.4, §5.2; `../../references/02-design-of-design.md` §1.4, §3.2, §3.4, §3.9.
- **External**: 45ck/software-architecture-skills (ADR template and decision-log practice); Brooks, *The Design of Design* (Spiral model with contracting points, constraint classification).

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
