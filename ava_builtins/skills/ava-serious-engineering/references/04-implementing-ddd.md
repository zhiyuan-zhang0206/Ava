# 04 · Implementing Domain-Driven Design

**Author**: Vaughn Vernon (the IDDD "red book" — the implementation manual produced by years of hard-won practice with Evans' "blue book")
**Position**: When a system's complexity comes from the business itself rather than technology, technical elegance fails — **domain complexity can only be fought with domain modeling**.
**Review status**: ⏳ Detail level per user-approved 01 benchmark; content pending user review (2026-08-05)
**Division of labor with 05**: this file carries **Vernon's implementation rules** — tactical building blocks, architecture patterns, how to apply DDD in code. **Evans' conceptual philosophy (05)** carries the why: modeling, Knowledge Crunching, the philosophy of Bounded Context. Where a concept's rationale lives in 05, this file says "see 05 §X" and keeps only the implementation angle.

---

## 0. One-Sentence Core

> Identify **bounded contexts**; model inside each one with a ubiquitous language; let contexts talk through events and anti-corruption layers — that is the whole secret of fighting domain complexity.

---

## 1. What Problem Does DDD Solve?

**Technical complexity** (hardware, parallelism, numerical stability) is solved by technical means; **domain complexity** ("what is a legal state transition of an order?", "do refunds or shipping have the last word?") cannot be solved by better frameworks — **only by domain modeling**. (This two-way split is consistent with Ousterhout's definition of complexity in 01 §1: domain complexity is a major source of the dependencies and obscurity he formalizes — DDD is one systematic way to reduce it.)

DDD gives you two toolkits:
- **Strategic design**: slicing the system at large scale — "how many independent worlds does the system consist of, and how do they talk?"
- **Tactical design**: modeling inside each world — entities, value objects, aggregates, services, repositories, domain events

> **Terminology note**: "Strategic/Tactical" here means DDD's two design levels. Do not confuse with Ousterhout's strategic-vs-tactical *programming attitude* in 01 §2 — same words, different axes.

---

## 2. Strategic Design (the most valuable part)

### 2.1 Ubiquitous Language (Vernon's extension — concept in 05 §1.3)
Evans anchors the language in code names; Vernon widens it to the whole delivery pipeline: **developers, product, domain experts, code, DB tables, APIs, docs, tests — must use one vocabulary, precise to the point of no ambiguity**. Typical anti-pattern: business says "user" while the code has `User`/`Customer`/`Account`/`Member` and nobody can say how they differ; "order complete" means payment to sales, shipping to the warehouse, reconciliation to finance. **Every concept has one definition; same word with different meanings = two different concepts = split into different contexts.**

### 2.2 Bounded Context — the book's most important concept (philosophy in 05 §4.1)
A Bounded Context is an independent, self-consistent language world; every word inside has one definition; across the boundary the same word may mean something entirely different. This section adds the **implementation angle** — what a boundary looks like when it is lived in code:

Classic example: "Product" in e-commerce
- Catalog context: name, image, specs, SEO
- Inventory context: SKU, location, quantity, safety stock
- Pricing context: pricing rules, promotions, tax

Newcomers build one giant `Product` class with all fields and the whole system is dragged down by it. DDD: **accept these as three different Products, model each separately, evolve independently**, linked by ID, synchronized by events when needed. Practical signals that a context boundary is needed: two teams use the same word with different meanings; two parts of the system mutate the same rows with different invariants; one model's change forces another team's release.

### 2.3 Subdomains and the Core Domain
(Evans calls the same idea "Distillation" — 05 §4.3; this is the classification with the investment table.) Think of the business as a map with three kinds of territory:

| Type | Definition | Investment strategy |
|---|---|---|
| **Core Domain** | the company's moat | best people, most rigorous DDD, build in-house |
| **Supporting** | needed but not differentiating | average quality is fine |
| **Generic** | everyone needs it (auth/logging/notifications) | buy or open source |

**Many teams spend 80% of effort on generic subdomains and rush the core domain.** Strategic thinking must come first.

### 2.4 Context Map (canonical relationship list)
Eight relationships between contexts; remember the most useful:

- **Partnership**: two contexts live or die together; teams coordinate
- **Customer-Supplier**: upstream defines the interface, downstream adapts, but downstream needs go on the upstream's plan
- **Conformist**: downstream fully adapts to upstream, which ignores it (integrating a third-party API)
- **Anticorruption Layer (ACL — the most important)**: **your context does not use upstream concepts directly; build a translation layer mapping them into your own** — prevents an external rotten model from polluting your core model
- **Open Host Service / Published Language**: expose a stable, documented protocol
- **Shared Kernel**: share a small piece of code — **very dangerous, use sparingly**; safe only when the shared part is tiny, stable, and the two teams coordinate tightly
- **Separate Ways**: explicitly decide not to integrate
- **Big Ball of Mud**: acknowledge the swamp, quarantine it, no new code enters

---

## 3. Architecture

### 3.1 Hexagonal Architecture (Ports and Adapters) — strongly recommended
- Center: the Domain Model, **which does not know the outside world exists**
- Middle: Application Services, orchestrating domain operations
- Outer: Adapters, talking to HTTP/DB/message queues/external APIs
- The Domain declares what it needs via Ports (interfaces); Adapters implement them

Why it's good: testable (Domain is pure functions + data structures; unit tests need no DB/network), replaceable (switching Postgres changes only an Adapter), multi-entry (REST/gRPC/CLI/message queues share one business logic). **When not to bother**: small CRUD systems where the "domain" is a thin wrapper over the database — the extra seams cost more than they save.

### 3.2 CQRS
Separate the write model from the read model: complex Aggregates guarantee write consistency; flattened view tables guarantee query performance.
**Use when**: read/write loads are strongly asymmetric, or read models must aggregate across Aggregates.
**Don't use when**: plain CRUD with one shape of access — CQRS adds a synchronization burden for no payoff.

### 3.3 Event Sourcing
Don't store current state; store the event sequence that produced it; current state = replay.
**Vernon's core advice: ES is not a default option.** It earns its keep when you genuinely need audit trails, temporal queries ("what did the order look like last Tuesday?"), or the ability to rebuild projections. Its costs: event schema evolution is a permanent discipline, snapshots are needed for performance, and every handler must tolerate replay. Consider it per-aggregate, not per-system.

### 3.4 Saga (long-running transactions)
Cross-context business processes orchestrated by event chains; each step's failure triggers compensating events. Two implementation styles: **orchestration** (one coordinator drives each step) and **choreography** (each step reacts to events and emits the next). Requirements: every step must be **idempotent** (safe to retry), and compensations must undo partial success. Minimal example: order → reserve stock → charge card; if charging fails, the stock reservation is compensated by a release event.

---

## 4. Tactical Design (the building blocks)

### 4.1 Entity (concept in 05 §2.2)
An object with a unique identity; identity unchanged = same object even if every attribute changes. Anti-pattern: ORM-driven code makes everything an Entity — a system full of mutable ID-bearing objects that are hard to reason about.

### 4.2 Value Object — use it more than Entity
No identity, defined by attribute values, **immutable**. `Money(amount, currency)`, `Address`, `DateRange`. Why it matters: immutable → no side effects → easy concurrency, testing, caching; expressive → `transfer(Money from, Money to)` is far safer than `transfer(double, double)`.
**Vernon's stance (stronger than Evans' — 05 §2.2): default to Value Objects; use an Entity only when the concept truly needs a persistent identity and a lifecycle.** Python's `dataclass(frozen=True)` / pydantic `BaseModel(frozen=True)` are natural VOs.

### 4.3 Domain Service
When an operation doesn't naturally belong to any Entity/VO, put it here (`TransferService.transfer(a, b, amount)`). **Stateless**, verb-named, parameters/returns are Entities/VOs. Keep it distinct from Application Services (orchestration) and Infrastructure Services (technical).

### 4.4 Domain Event — emphasized by Vernon (Evans' seed: 05 §2.3)
A "happened, business-meaningful" fact, **named in the past tense**: `OrderPlaced`, `PaymentReceived`. Characteristics: immutable, carries context data (not just an ID), produced by an Aggregate while executing a command, published to subscribers.
**Why it's central**: decoupling (announce "what I did", don't care who responds), cross-context integration (subscribe rather than call directly), auditability, and it underpins ES/CQRS. Domain events are a powerful mechanism for decoupled collaboration between Bounded Contexts — one integration style among several (REST and messaging also have their places), but the one that keeps contexts most independent.

### 4.5 Module
Package by sub-topic within a Bounded Context. Naming reflects the Ubiquitous Language: **good**: `order/`, `payment/`, `shipping/`; **bad**: `controllers/`, `models/`, `services/` (technical slicing is an anti-pattern).
**When a sub-topic should become its own Bounded Context**: when the two "sub-topics" start developing different Ubiquitous Languages — the same word meaning different things on each side — that is the signal to split; otherwise keep it a module.

### 4.6 Aggregate — the hardest part of tactical design (concept in 05 §2.3)
A group of related Entities/VOs modified as a whole, with one Aggregate Root; external code can only reach inner objects through the Root. **Core rules (Vernon repeats these)**:
1. **One transaction modifies one Aggregate** — needing atomic changes to two Aggregates means the boundary is wrong, or you need eventual consistency (events + Saga)
2. **Design Aggregates small** — the most common beginner mistake is enclosing the whole graph in a giant aggregate, causing concurrency conflicts and performance collapse
3. **Reference across Aggregates by ID, not object reference** — `Order` stores `customerId`, not a `Customer` object
4. **Strong consistency inside, eventual consistency between**

How to find boundaries: ask "which data must this business rule keep consistent at once?" — that circle is an Aggregate.

### 4.7 Factory
Encapsulate complex creation so objects are born valid (not `new` followed by a pile of setters).

### 4.8 Repository
An interface that behaves like "an in-memory collection of objects," backed by a DB. Key points: **one Repository per Aggregate Root** (not per Entity); the interface belongs to the Domain layer, the implementation to Infrastructure; it is not a DAO (DAO exposes SQL-think CRUD; Repository exposes collection-think queries).

### 4.9 Application Layer
A thin layer above the Domain: receive request → load Aggregates → call methods to execute business logic → save → publish events → return. **Contains no business logic** — all business logic lives in the Domain.

---

## 5. DDD Is Not a Silver Bullet

**Costs**: deep involvement of domain experts, long modeling/refactoring investment, overkill for small CRUD systems.

**Suitable**: high business complexity (many rules, states, long flows), long-lived systems (>2 years), teams willing to keep modeling.
**Unsuitable**: content management/blogs/simple backends; complexity mainly technical (HPC, numerical computing); early-stage MVPs.

**Decision rule: if the core pain is "business rules are too tangled to reason about" — DDD fits; if the pain is "performance, concurrency, algorithms" — DDD will not help.**

---

## 6. One Diagram to Remember the Whole Book

```
Strategic design (decides the cuts)
├── Ubiquitous Language (one language per context)
├── Bounded Context (independent language worlds)
├── Subdomain classification (Core / Supporting / Generic)
└── Context Map (ACL, OHS, Customer-Supplier...)

Architecture (decides the shape)
├── Hexagonal (default recommendation)
├── CQRS (read/write separation)
├── Event Sourcing (rebuild state from events)
└── Saga (cross-context collaboration)

Tactical design (decides the code)
├── Entity (has identity) / Value Object (no identity — prefer)
├── Domain Service / Domain Event
├── Aggregate (consistency boundary; one transaction per aggregate)
├── Factory / Repository
└── Application Service (thin orchestration)
```

---

## 7. Relationship to the Skill Architecture

- **Bounded Context / Ubiquitous Language** → core sources of heuristic ① (concepts are boundaries) + ⑨ (naming is the contract)
- **ACL / event-driven** → integration patterns for heuristic ⑤ (process boundaries are trust boundaries)
- **Aggregate consistency boundary** → heuristic ③ (single source of truth) applied to state management
- **Value Object first / hexagonal** → implementation techniques for heuristic ⑦ (isolate points of change)

**See also**: 01 §1 Complexity (definition; domain vs technical complexity) · 05 §1.3 Ubiquitous Language (Evans' original, code-anchored definition) · 05 §4.1 Bounded Context (the philosophy) · 05 §4.3 Distillation (= Subdomain Classification) · 05 §2.2 Entity/VO (the identity test) · 05 §2.3 Aggregate (why boundaries exist)
