# 05 · Domain-Driven Design

**Author**: Eric Evans (2003; the "Bible" of software design, the blue book)
**Position**: Not about architecture (not distributed systems, not microservices) — about **how to think, how to talk with domain experts, and how to encode "understanding" directly into the system**. The hardest part of software is not writing code but figuring out "what we are actually doing."
**Review status**: ⏳ Detail level per user-approved 01 benchmark; content pending user review (2026-08-05)
**Division of labor with 04**: this file carries **Evans' conceptual philosophy** (modeling, language, strategic design as ways of thinking). **Vernon's implementation manual (04)** carries the tactical rules and architecture patterns — where a concept has an implementation treatment, this file says "see 04 §X" instead of repeating it.

---

## 0. One-Sentence Core

> Model, language, boundaries — the three weapons engineers wield against complexity. **The model is distilled domain knowledge; code is the model's executor; a ubiquitous language makes the model discussable; boundaries keep the model from collapsing.**

---

## 1. Three Core Ideas of Modeling

### 1.1 The Model = Distilled Domain Knowledge (not a UML diagram)
"Model" is not a class diagram, not a DB schema, not an API spec. It is **a set of carefully chosen and structured concepts expressing an understanding of a domain** — the team's consensus on "how the business works," distilled to what matters and stripped of what does not. A model that cannot drive the code is decoration.

### 1.2 Knowledge Crunching — a continuous loop
Not a one-off "requirements analysis," but **repeated dialogue with domain experts: sketch, get corrected, re-sketch**. The PCB design example: the author knew no electronics; through dialogue — "component" refined into "component instance," the core concept of "net (wire network)" emerged, "topology" turned out ignorable in some cases.
**Key insight: domain experts often cannot articulate their own work. The model is *extracted* by developer and expert together.**

### 1.3 Ubiquitous Language — the most underestimated concept
The whole team (developers + business + designers) must use one vocabulary, strictly corresponding to class names, method names, module names in the code.
- Anti-example: business says "ticket", code writes `Ticket`, PM docs say "task" — every meeting requires mental translation, and translation loss is the biggest source of hidden bugs
- Positive: every core noun the business uses must have a same-named class; class names translated back must be understandable and correctable by the business; ambiguity triggers an immediate alignment meeting + synchronized rename
- Signal: if "task" means both "user requirement" and "internal execution step" — split into two concepts (UserRequest vs ExecutionStep)

> **Vernon's extension (04 §2.1)**: the language must cover the full delivery pipeline — DB tables, APIs, docs, tests — not just code names. Same principle, wider net.

### 1.4 Model-Driven Design — bidirectional binding
Model and code **must bind bidirectionally**: model changes force code changes; an awkward abstraction in code usually means the model itself is wrong — fix the model. Many teams run "analysis model vs design model" as two separate things (analysts draw UML, developers write something else) — **an analysis model divorced from the implementation loses value instantly.**

**Operational steps for the binding**:
1. **Recognize the signal** — what counts as "awkward"? A method name containing "And"/"Or", a parameter list longer than four, a class referenced by both UI and DB code
2. **Trace back to the model** — which business concept does this abstraction correspond to in the Ubiquitous Language? Ask the domain expert: "what is this operation called in the business?"
3. **Fix the model before the code** — redraw the model on paper/whiteboard, validate it through Knowledge Crunching, then change the code

---

## 2. Tactical Building Blocks (the most practical part)

### 2.1 Layered Architecture
| Layer | Responsibility |
|---|---|
| User interface | present info, interpret commands |
| Application | orchestrate tasks, **no business rules** (thin) |
| Domain | business concepts, rules, state — **DDD's core battlefield** |
| Infrastructure | persistence, messaging, external APIs |

**Core principle: the Domain layer must not depend on any upper layer or the infrastructure layer** — it should be "pure" business logic, theoretically runnable without DB/network/UI. Abstract external service calls behind interfaces (defined in the Domain), with implementations in Infrastructure — switching providers or databases does not touch core logic. (Vernon's Hexagonal Architecture in 04 §3.1 is the modern concrete form of this same principle.)

**Smart UI anti-pattern**: business logic written directly in UI event handlers. Fine for simple CRUD; death once the domain gets complex.

### 2.2 Three Domain Objects: Entity / Value Object / Service
The single distinguishing question: **does it have identity?**

- **Entity**: unique identity + lifecycle. It stays itself even if every attribute changes (User, Order). Must have an ID; equality by ID
- **Value Object**: no identity, immutable, defined by attributes. `Money(100, USD)`, `DateRange`. **Prefer VO over Entity whenever possible** — no lifecycle, concurrency, or persistence concerns (Vernon pushes this further: "default to VOs" — see 04 §4.2)
- **Service**: for operations belonging to no Entity/VO. Stateless, verb-named, parameters/returns are Entities/VOs. Judgment: an operation needing the entity's state goes on the entity; a pure "input → output" operation goes in a service — don't pile everything into a god class

### 2.3 Aggregate (concept) / Factory / Repository (see 04)
- **Aggregate**: a boundary around a cohesive group of objects, with one root Entity (Aggregate Root). External code references only the root; internal objects change only through it (modify OrderLine via `order.updateLine(...)`). **Why: transaction boundary + invariant protection** ("order total = sum of lines" is maintained only inside the aggregate); cross-aggregate consistency is eventual. Is Order + OrderLine one aggregate or two? It depends on whether "adding a line must atomically update order state" — this decision directly determines your concurrency model and DB design. (Vernon's four implementation rules — one transaction per aggregate, small aggregates, reference by ID, strong-inside/eventual-between — are in 04 §4.6.)
- **Factory**: encapsulate complex creation; constructors guarantee objects are born valid
- **Repository**: pretend all objects live in memory. `OrderRepository.findById(id)` hides SQL/NoSQL/RPC underneath. **Only for aggregate roots.** (Full treatment: 04 §4.8.)

---

## 3. Modeling Is Iterative

- **Make implicit concepts explicit**: a pattern or constraint recurring in code (orders over $10k need special approval) → an unnamed implicit concept → extract an explicit object (`ApprovalPolicy`), and the code suddenly clarifies
- **Supple Design** (seven patterns in the book; the four below are the core, each with a bad → good pair):
  - **Intention-revealing interfaces**: method names state intent, not implementation — bad: `process(data)`, good: `calculateOverdraftFee(account, amount)`
  - **Side-effect-free functions**: prefer pure functions — bad: `order.setStatus(Shipped)` that also sends email; good: `order.markShipped()` returns an `OrderShipped` event, the email is sent by a subscriber
  - **Assertions**: express constraints via contracts (pre/postconditions, invariants) so the model's rules are machine-checkable
  - **Conceptual contours**: cut modules along the domain's "natural grain" — the seams of the model, not the seams of the framework
  - **Standalone classes** (low coupling, high cohesion at the class level): a class that needs no context to understand is easier to reason about
  - **Closure of operations**: an operation whose result lives in the same set as its inputs — `add` closes over numbers, `merge` should close over Messages
  - **Declarative design** (Evans' forward-looking pattern): express the model as rules/constraints that a machine can execute — the intent is what you write, not the control flow
- **Breakthrough**: modeling is "linear progress + occasional leaps" — one day you realize "the real core concept is X" and the whole design is redone. **This is good, not failure; don't refuse to refactor because "too much code is written".** Evans' practical discovery techniques: (1) listen to the language — concepts the business keeps using that have no home in the model; (2) look for awkwardness — repeated workarounds in the code; (3) contemplate contradictions — two parts of the model assuming different things about the same concept

---

## 4. Strategic Design (most relevant for large systems)

### 4.1 Bounded Context — the most important concept in the book, period
**A model is valid only inside its Bounded Context. Across contexts, the same noun can mean entirely different things.**

"Customer" in e-commerce:
- Sales context: name, email, cart, order history
- Logistics context: shipping address, delivery preferences
- Support context: history, VIP tier, sentiment labels

Wrong: one unified `Customer` class everywhere, ending at 200 fields nobody dares touch. Right: each context has its own Customer model, communicating through explicit translation layers. (Vernon's "Product" walk-through and the evolution/ownership angle: 04 §2.2.)

### 4.2 Context Map
Draw all Bounded Contexts and their relationships:

| Relationship | Meaning |
|---|---|
| **Partnership** | two contexts live or die together; teams coordinate (Vernon's list adds this and Big Ball of Mud — 04 §2.4 is the canonical list) |
| Customer/Supplier | one depends on the other; downstream can influence upstream priority |
| Conformist | downstream forced to copy upstream's model |
| **Anticorruption Layer** | **a translation layer keeping external rotten models out — a golden concept** |
| Open Host Service | a set of open protocols for any downstream |
| Published Language | an agreed standard (JSON schema, protobuf) for integration |
| Separate Ways | don't integrate at all |
| **Big Ball of Mud** | the acknowledged swamp — quarantine it, no new code enters (Vernon's addition; Evans frames the need for boundaries) |

Especially useful with external systems: provider APIs have inconsistent styles; an ACL keeps your core code stable.

### 4.3 Distillation
Don't give every part of the system equal effort (Vernon calls the same idea "Subdomain Classification" — 04 §2.3; same concept, same three-way split):
- **Core Domain**: the competitive advantage; best people, repeated polishing
- **Generic Subdomain**: buy or open-source (auth, billing)
- **Supporting Subdomain**: good enough is fine

**Your Core Domain is the differentiated business logic itself — a generic framework is probably not it (unless you build frameworks); common plumbing is Generic — don't spend a year building your own framework.**

### 4.4 Large-Scale Structure
When the system grows beyond many Bounded Contexts, higher-level organizing principles are needed. Evans offers four patterns (he is candid that this part of the book is the least mature — treat as a menu, not a mandate):
- **System Metaphor**: one shared image of the system's operation that orients everyone (e.g. "the checkout is a pipeline of stations")
- **Responsibility Layers**: an overall layering of the system into broad responsibility bands, each with its own design and vocabulary
- **Knowledge Level**: split the model into two levels — the rules and the objects they describe — so rules can be changed like data
- **Pluggable Component Framework**: abstract the interactions between contexts so implementations can be plugged in and swapped

---

## 5. Concrete Advice

1. **Before designing a system, list all core nouns** and define what each means in which Context — you will usually find 30% of the chaos is naming inconsistency
2. **Force code to speak the business's language**: if business says "task", don't have Job/Task/Workflow in code
3. **Identify Aggregate boundaries**: are Order and OrderLine one aggregate or two? This decides your transaction model (→ 04 §4.6 for the rules)
4. **Isolate fast-changing external systems behind an ACL** — they iterate faster than your core; don't let their changes infect it
5. **Find your Core Domain** and point the smartest minds at it
6. **Accept that modeling is iterative** — you won't design it right the first time; budget for refactoring

(These six are the "how to practice" layer of the heuristics in §7 — the mapping is bidirectional.)

---

## 6. Limitations of the Book (for fairness)

- Examples heavily Java + relational DB + OO — translate when reading code samples
- Written before microservices / event-driven became mainstream — ES/CQRS not covered (Vernon's red book, 04, is the modern complement: events at 04 §4.4, ES at 04 §3.3)
- Evans can be verbose, circling the same point from multiple angles

---

## 7. Relationship to the Skill Architecture

- **Model = distilled knowledge / Model-Driven Design** → direct source of heuristic ② (code model ∝ conceptual model); philosophical root of the Core Principle (code as executable projection of the conceptual model)
- **Ubiquitous Language** → heuristic ⑨ (naming is the contract)
- **Bounded Context / Context Map** → the domain-modeling side of heuristics ① (concepts are boundaries) + ⑤ (process boundaries are trust boundaries)
- **Supple Design (intention-revealing / side-effect-free / assertions)** → modeling techniques for heuristics ⑩ (tests anchor concepts) + ④ (explicit over implicit)
- **Distillation** → heuristic ⑧ (make deletion cheap and visible): don't build non-core things yourself

**See also**: 01 §1 Complexity (definition; domain complexity is a major source of the dependencies/obscurity Ousterhout formalizes) · 01 §3.1 Deep Modules (module design at the code level) · 03 Tip 74 Naming + Tip 80 Glossary (Ubiquitous Language's mechanics) · 04 §2.4 Context Map (canonical relationship list) · 04 §4.6 Aggregate (implementation rules) · 04 §4.4 Domain Events (Evans' seed, Vernon's full treatment)

> **Terminology note**: "Strategic/Tactical" here means DDD's two design levels (context-slicing vs building blocks). Do not confuse with Ousterhout's strategic-vs-tactical *programming attitude* in 01 §2 — same words, different axes.
