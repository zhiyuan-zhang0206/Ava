---
name: conceptual-integrity
description: "Restores a coherent design concept across APIs, terminology, and features. Use when a system feels designed by committee, exposes inconsistent interfaces, uses one word differently across modules, or accumulates features that do not belong together."
---

# Conceptual Integrity

## One-Sentence Core
> The mark of a great design is conceptual integrity — unity, economy, clarity — and it comes from one authorized chief designer controlling the whole, never from committee negotiation.

## Core Principles

- **Conceptual integrity is the most important property of a system**:A system with conceptual integrity feels coherent — every part fits, every interface follows the same philosophy, and users can predict how unfamiliar features will behave. Without it, even a technically sound system is confusing and expensive to maintain. — **Why**:Brooks studied both great designs (Wren's St. Paul's, Seymour Cray's CDC 6600, Unix) and mediocre ones (committee-designed products) and found the difference is always the same: great systems had one mind enforcing a unified vision; mediocre systems were negotiated compromises. — **How**:Before adding any module, ask: "Does this fit the Design Concept, or does it introduce a new idea that has no precedent in the system?" If it doesn't fit, either the module is wrong or the Design Concept needs updating — never let both drift simultaneously.

- **One coherent vision controls the concept — and if that vision is yours, the discipline is yours**:Brooks' finding is organizational — great designs come from one authorized chief designer, not committee negotiation. But this skill must be executable by whoever reads it, including an agent working alone. The organizational form (one architect, one surgical team) is the *why*; the *how* is a discipline that does not require anyone to appoint you: — **Why**:Equal negotiation yields committee design — bloated, mediocre, nobody dares say no. Brooks explicitly rejects the romanticized "design as interdisciplinary negotiation"; a System Architect must be authorized to act as user's agent, approver, and advocate with a clear vision. — **How**:(1) **Write the Design Concept down** — a concept that lives only in someone's head cannot be checked, by humans or by agents. (2) **If you are the designer, act as the authorized chief designer**: be the one who says "this doesn't fit" — do not wait for a committee to grant permission, and do not dilute the concept to please stakeholders; collect their constraints as input, not as design votes. (3) **If you are not the designer, find who owns the concept for this boundary** and route design decisions through them; never patch around the owner. (4) For a multi-team system, each Bounded Context has its own concept owner; cross-context consistency is maintained through explicit context maps, not shared ownership.

- **The Design Concept is the team's "Platonic ideal system"**:Every artifact has three layers: Idea (conceptualization) → Implementation → Interaction. The Design Concept is the unifying vision — more "real" than any specific implementation — that all discussion returns to. — **Why**:Brooks argues that the Design Concept's clarity and the team's consensus on it directly determine how elegant the product is. When the concept is fuzzy, every micro-decision becomes a separate negotiation; when it is sharp, a thousand decisions make themselves. — **How**:Write down the Design Concept in one page: what the system is, what it is not, the core metaphor or organizing idea, and the top 3–5 principles every component must follow. Test every design review against it. When the concept itself needs to change, update the document first, then refactor the code to match — never the reverse.

- **Conceptual integrity in code is a checklist, not a feeling**:Brooks argues the case at the organizational level; the code-level translation of "one concept, consistently applied" is operational. — **Why**:A Design Concept that lives only in the architect's head cannot be verified. The following checklist (§2.1 from Brooks, translated to code operations) makes integrity testable. — **How**:For every new module or interface change, run these checks: (a) Does the module's interface fit the Design Concept? If not, the module is wrong or the concept needs updating — never both silently. (b) Test with Ousterhout's "deep module" lens: does the interface expose details that should stay hidden? (c) Test with ETC (Easier To Change): will this design accommodate the concept's evolution? (d) Is the API surface controlled by one coherent vision? When two parts of the system use the same concept with different shapes, that is a conceptual-integrity defect, not a local style choice.

- **The user interface (or API surface) must be controlled by one person**:If an architect cannot control it, a user certainly cannot. — **Why**:The interface is what the user perceives as "the system." Inconsistency at the interface level means the user must learn multiple mental models for what should be one system. Brooks is explicit: the UI must have a single owner. — **How**:Audit the API surface (or UI) for the same concept appearing with different names, shapes, or behaviors. For an API, check: do all list endpoints use the same pagination scheme? Do all error responses follow the same structure? For a UI, check: does "save" mean the same thing on every page?

- **Small teams preserve integrity; large teams destroy it without discipline**:Brooks studied collaboration models — architect + master builder (Wren), surgical team, chief programmer team — and the pattern is always one person's coherent concept executed with help, never joint design by committee. — **Why**:Every additional mind that has veto power over the concept dilutes it. The surgical team model works because one surgeon owns the operation; the team enables, not designs. — **How**:Keep the design team for any one Bounded Context to 1–2 people who think as one. Grow the implementation team, but protect the design owners from second-guessing and interruption. When more designers are needed, split the system into independent contexts, each with its own integrity — don't add designers to one context.

## Checklist

- [ ] **SHOULD** Is there a written Design Concept (one page: what the system is, what it is not, organizing metaphor, top 3–5 principles) that the whole team can point to?
- [ ] **MUST** Does every new module's interface demonstrably fit the Design Concept? If one doesn't, was the concept updated before the code landed?
- [ ] **SHOULD** Is the API surface consistent — same concept, same shape everywhere? (Audit: list endpoints, error formats, auth patterns — are they uniform?)
- [ ] **MUST** When two parts of the system use the same word, do they mean the same thing? When they mean different things, are they in different Bounded Contexts?
- [ ] **SHOULD** Is there one person (or a pair operating as one mind) authorized to say "this doesn't fit the concept" — and is that person actually doing it?
- [ ] **MUST** Does a change to the Design Concept propagate cleanly? Test: pick a concept-level change and trace how many places it touches. If the answer is "uncountable," integrity has broken down.
- [ ] **SHOULD** Are design meetings reviews of proposals against the concept, or are they collective design sessions? (The former preserves integrity; the latter erodes it.)

## Anti-Patterns

- **Design by committee**:Every stakeholder gets a say in the design; the result is a bloated compromise that satisfies everyone and pleases no one. → alternative: One authorized designer owns the concept; stakeholders provide constraints and desiderata, not design decisions.
- **Silent concept drift**:The code evolves away from the Design Concept without the concept being updated — or the concept changes without the code being refactored. Both directions create a gap that widens with every change. → alternative: When a module doesn't fit, either reject the module or update the concept document first, then refactor.
- **Concept collision**:Two parts of the system use the same concept (e.g., "User") with different shapes, behaviors, or lifecycles — and nobody has declared them separate Bounded Contexts. → alternative: Either unify the concept (if they mean the same thing) or split them into separate contexts with explicit translation (if they don't).
- **API surface anarchy**:Different endpoints use different pagination, different error formats, different auth patterns — the system has no single owner of the interface. → alternative: One person owns the API surface design; every endpoint conforms to the same conventions or has a documented, justified exception.

## Examples

**Example 1: Committee Design vs Single Vision**

❌ Bad (committee design):
The team designs a user management system. Product wants role-based access. Engineering wants attribute-based access. Security wants mandatory access control. The compromise: all three, with a configuration flag to switch. The result is a system where nobody — including users — can predict how permissions resolve.

✅ Good (single-vision design):
The System Architect evaluates all three models against the core use case. The product is an internal tool with simple hierarchies → role-based access is chosen. The other models are documented as future considerations, not implemented. Every permission check in the system works the same way.

**Example 2: Concept Collision in API**

❌ Bad (same concept, different shapes):
```
POST /api/users          # body: { "name": "Alice" }
POST /api/admin/users    # body: { "fullName": "Alice" }
GET  /api/users/123      # response: { "name": "Alice", "id": 123 }
GET  /api/admin/users/123 # response: { "userName": "Alice", "userId": 123 }
```
Two endpoints for the same concept "User" with inconsistent field names, URL patterns, and response shapes. The user must learn two mental models.

✅ Good (consistent API surface):
```
POST /api/users          # body: { "name": "Alice" }
GET  /api/users/123      # response: { "id": 123, "name": "Alice" }
# Admin operations use the same User model; admin-specific fields go in a separate AdminContext
```

**Example 3: Concept Drift**

❌ Bad (silent drift):
The Design Concept says "every entity has a single owner." Six months later, a feature adds shared ownership for Documents — but the concept document is never updated. New team members read the concept, implement single-owner for Folders, and the system now has two contradictory ownership models with no explicit choice.

✅ Good (concept-first evolution):
When shared ownership is needed, the architect updates the Design Concept: "Entities have one or more owners; Documents may be shared; Folders remain single-owner for simplicity." The concept change is reviewed, agreed, and then the code follows.

## Relationships

- `principles/complexity-management` — Conceptual integrity is the organizational defense against complexity: one coherent concept prevents the fragmentation that breeds dependencies and obscurity. Ousterhout's "obviousness" and "consistency" (§5.3) are the code-level expression of conceptual integrity.
- `principles/ubiquitous-language` — The Ubiquitous Language is conceptual integrity applied to naming: every concept has one name, and that name means exactly one thing across the whole delivery pipeline.
- `principles/bounded-context` — When conceptual integrity cannot span the whole system (multi-team, multi-product), Bounded Contexts draw the lines where one concept ends and another begins.
- `practices/design` — The Design Concept is the primary output of the design phase; conceptual integrity is the standard by which design quality is judged.
- `practices/review` — Code review's highest-order question: "Does this change fit the Design Concept?"
- `references/02-design-of-design.md` §1–§2 — Full treatment of the Design Concept, collaboration models, and conceptual integrity in code.

## Sources

- Brooks, *The Design of Design* — conceptual integrity, Design Concept, authorized chief designer, collaboration models (surgical team, chief programmer team), process vs greatness. Primary source for this skill.
- Brooks, *The Mythical Man-Month* — the original statement of conceptual integrity and the surgical team model.
- Ousterhout, *A Philosophy of Software Design* — deep modules, consistency, obviousness (the code-level expression of conceptual integrity). See `references/01-philosophy-of-software-design.md` §3.1, §5.3.
- Thomas & Hunt, *The Pragmatic Programmer* — ETC (Easier To Change) as the test of whether a design supports conceptual evolution. See `references/03-pragmatic-programmer.md` Tip 14.
