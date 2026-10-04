---
name: supple-design
description: Refines rigid domain models into intention-revealing, change-friendly designs. Use when adding a feature requires surgery across many classes, method names expose implementation jargon, operations escape their domain, or tightly tangled parts resist change.
---

# Supple Design

## One-Sentence Core
> A supple design reveals its intent at a glance, isolates change to a single class, closes its operations within coherent sets, and makes its rules machine-checkable — it bends where the business bends and stays firm where the invariants hold.

## Core Principles

- **Intention-Revealing Interfaces Name the What, Not the How**: a method's signature must state what the caller wants to achieve in domain terms, not how the implementation achieves it — `calculateOverdraftFee(account, amount)` over `process(data)`. **Why**: Evans (05 §3) identified the gap between "what the code does" and "what the business asked for" as the primary source of obscurity (Ousterhout's second root cause of complexity, 01 §1.2). A method named `process` or `handle` tells the reader nothing about intent; the reader must read the implementation to understand what business operation it performs — and reading the implementation of every method to understand the system is exactly the cognitive-load symptom Ousterhout warned against. **How**: name every public method as a verb phrase in the Ubiquitous Language — if you cannot name it in a way a domain expert would understand, the operation itself may not belong in the model; audit method names during code review with the test: "can a domain expert read this name and say what business action it performs?"

- **Side-Effect-Free Functions Make Behavior Predictable**: a function that reads state and returns a value without mutating anything (a "query" in CQRS terms) can be called anywhere, composed freely, tested in isolation, and reasoned about without tracing state changes. **Why**: Evans (05 §3) positioned side-effect-free functions as the bridge between the model and the implementation: when business rules are expressed as pure functions, the model's assertions become executable and the code becomes the model's proof. Ousterhout (01 §1.2) identified hidden side effects as a primary cause of "unknown unknowns" — the worst complexity symptom, where change A breaks B and nobody could have known in advance. **How**: separate commands (mutate state, return nothing or events) from queries (return data, mutate nothing); push side effects to the boundaries — a domain method that updates order state should return an `OrderShipped` event, not call `emailService.send()` directly; let an infrastructure subscriber handle the email.

- **Assertions Encode Invariants That Machines Can Check**: every business rule that must always hold — "an order total is the sum of its line items," "a shipped order cannot be cancelled" — must be expressed as an explicit, executable assertion, not as a comment or as tribal knowledge. **Why**: Evans (05 §3) listed assertions as one of the seven supple design patterns because they turn the model's constraints from documentation into enforcement. When invariants live only in comments, they rot; when they live only in the heads of senior engineers, they walk out the door. Ousterhout (01 §4.3, "Define Errors Out of Existence") added the complementary technique of redefining semantics so violations become impossible — for the rest, assertions are the next best defense. **How**: express preconditions, postconditions, and invariants as code — `assert order.total == sum(line.subtotal for line in order.lines)` at the Aggregate boundary, or type-system constraints (`NonEmptyList`, `PositiveAmount`); enforce them in the Aggregate Root's mutating methods so that an invalid state cannot be persisted.

- **Conceptual Contours Follow the Domain's Natural Grain**: cut modules not along technical seams (controllers, models, services) but along the domain's own joints — the places where the Ubiquitous Language naturally splits, where one concept's change rarely forces another's, where the business itself draws a line. **Why**: Evans (05 §3) observed that a model forced into technical buckets (all "services" in one package) buries the domain's structure under framework scaffolding. When modules match the domain's own decomposition, adding a business feature means touching one module, not three; when they match the framework's decomposition, every feature scatters across the codebase. Ousterhout (01 §3.2) described the same phenomenon as "temporal decomposition" — the most common information-leakage pattern, where modules split by execution order rather than knowledge domain. **How**: organize code by business capability (order/, payment/, shipping/) rather than by layer (controllers/, models/, services/); when a sub-topic within a module starts developing its own Ubiquitous Language — the same word meaning different things on each side — that is the signal to split into its own Bounded Context (Vernon 04 §4.5).

- **Standalone Classes Are Self-Explanatory**: a class that can be understood without reading any other class — whose dependencies are few, whose collaborators are obvious from its interface, and whose behavior does not depend on implicit global state — is the unit of suppleness at the smallest scale. **Why**: Evans (05 §3) defined standalone classes as low coupling and high cohesion at the class level: the less context a reader needs to understand a class, the easier it is to modify correctly. Ousterhout's "deep module" (01 §3.1) is the interface-level complement: a deep module hides complexity behind a simple interface; a standalone class hides internal logic behind a self-contained boundary. **How**: count the number of imports in each class — every import is a dependency; aim for classes that import abstractions (interfaces), not concrete implementations; a class whose test requires 10 mocks is not standalone — it knows too much about the system.

- **Closure of Operations Keeps a Concept Whole**: an operation is "closed" if its result lives in the same set as its inputs — `add` on numbers returns a number; `merge` on documents should return a document, not a void mutation. **Why**: Evans (05 §3) used closure of operations to define the natural scope of a concept: when an operation returns a value of the same type, it composes — `a.add(b).add(c)` — and the concept stays self-contained. When an operation returns void and mutates state elsewhere, it leaks — the caller must track side effects outside the concept. **How**: prefer methods that return a new instance of the same type (`Money.plus(other) -> Money`) over methods that mutate in place; for operations that must produce side effects, return a Domain Event rather than calling an external service — the event is a value in the domain's type system, so the operation stays closed within the domain.

- **Declarative Design States Intent Without Control Flow**: express the model's rules as declarations of what must be true, not as sequences of how to enforce them — a specification pattern, a rule engine, or a set of constraints that a machine evaluates. **Why**: Evans (05 §3) presented declarative design as the forward-looking seventh pattern: when business rules change frequently and are complex, encoding them imperatively (if-else chains, state-machine transitions hand-coded) creates change amplification — every rule change requires tracing control flow. Declarative rules isolate each rule as data, so adding a rule means adding a declaration, not editing a branching method. **How**: for subsystems with many business rules (validation, pricing, eligibility), extract rules into a declarative form — a list of `Rule` objects each with a `satisfied_by(context) -> bool`, or a specification pattern; the engine evaluates them; the business stakeholder can read the rule declarations without reading engine code.

## Checklist
- [ ] **SHOULD** Can every public method name be understood by a domain expert without reading the implementation?
- [ ] **MUST** Are commands (mutate state) separated from queries (return data) — no method both changes state and returns a computed value?
- [ ] **MUST** Are the Aggregate's invariants expressed as executable assertions, not just comments?
- [ ] **MUST** Do module boundaries follow the domain's own concepts rather than technical layers?
- [ ] **SHOULD** Can each class be understood (and tested) without reading more than 2–3 other classes?
- [ ] **SHOULD** Do operations return values of the same type so they compose, rather than mutating state and returning void?
- [ ] **SHOULD** Are complex rule sets expressed declaratively so each rule can be read and changed independently?
- [ ] **MUST** When a side effect is necessary, is it expressed as a returned Domain Event rather than a direct call to infrastructure?

## Anti-Patterns
- **Opaque Method Names**: `process()`, `handle()`, `execute()`, `run()` — names that force the reader to inspect every implementation to understand what the system does. → **Alternative**: name every method with a domain verb phrase: `calculateOverdraftFee`, `approveLoan`, `reserveInventory`.
- **Mixed Command-Query**: a method that both mutates state and returns a value — `def ship(): return tracking_number` where `ship()` also updates order status. → **Alternative**: split into `def ship() -> OrderShipped` (returns event) and let the caller extract `tracking_number` from the event.
- **Comment-Only Invariants**: `// Note: order total must equal sum of line items` — enforced nowhere except developer memory. → **Alternative**: encode as `assert order.total == sum(line.subtotal for line in order.lines)` in the Aggregate Root's save path.
- **Technical Package Structure**: `controllers/OrderController`, `services/OrderService`, `models/Order` — the same domain concept scattered across three packages by technical layer. → **Alternative**: `order/OrderController`, `order/OrderService`, `order/Order` — all Order concerns in one package.
- **God Class with 50 Dependencies**: a class importing 30 other modules, needing 15 mocks to test — it carries the whole system in its head. → **Alternative**: extract standalone classes that each own one coherent responsibility; a class needing more than 5 injected dependencies is a candidate for decomposition.

## Examples

**Bad (Opaque interface)**:
```python
def process(data: dict) -> dict:
    if data.get("type") == "overdraft":
        fee = data["amount"] * 0.05
        return {"fee": fee}
```
The name `process` says nothing. The caller must know the magic string `"overdraft"` and the internal structure of `data`. Changing the fee calculation requires reading the implementation.

**Good (Intention-revealing)**:
```python
def calculate_overdraft_fee(account: Account, overdraft_amount: Money) -> Money:
    """Calculate the overdraft fee for exceeding the account balance.

    Fee is 5% of the overdraft amount, capped at the account's max_fee.
    """
    raw_fee = overdraft_amount * Decimal("0.05")
    return Money(min(raw_fee, account.max_fee_amount), account.currency)
```
The name states the business operation. The types (`Account`, `Money`) carry domain meaning. A domain expert can read the signature and verify correctness.

**Bad (Hidden side effect)**:
```python
def mark_shipped(order: Order) -> None:
    order.status = "shipped"
    email_service.send(order.customer_email, "Your order has shipped!")
    inventory_service.decrement(order.items)
```
`mark_shipped` sends an email and decrements inventory — two side effects invisible from the signature. Testing requires mocking two services. Changing the email template forces changes to the domain model.

**Good (Side-effect-free with events)**:
```python
def mark_shipped(order: Order) -> OrderShipped:
    order.status = OrderStatus.SHIPPED
    return OrderShipped(
        order_id=order.id,
        customer_email=order.customer_email,
        items=order.items,
        shipped_at=datetime.utcnow()
    )
```
The method mutates only `order` and returns a domain event. The caller or an infrastructure subscriber handles email and inventory — the domain method stays pure and testable. No mock needed for `email_service`.

## Relationships
- **principles/complexity-management**: intention-revealing interfaces and side-effect-free functions directly reduce obscurity (Ousterhout's second root cause, 01 §1.2); standalone classes and conceptual contours reduce dependencies (the first root cause).
- **principles/ubiquitous-language**: intention-revealing interfaces are the Ubiquitous Language made executable — method names are the language.
- **principles/bounded-context**: conceptual contours are the intra-context complement to inter-context boundaries; when a contour becomes a context is a judgment call (Vernon 04 §4.5).
- **principles/dependency-management**: standalone classes are dependency management at the class level; closure of operations reduces cross-class coupling.
- **practices/design**: supple design patterns are the refinement phase of modeling — applied after the initial model is in place.
- **references/05-domain-driven-design.md §3**: Evans' seven supple design patterns.
- **references/01-philosophy-of-software-design.md §3.1**: Deep Modules — the interface-level complement to supple design; §3.2 Information Hiding.

## Sources
- Evans, *Domain-Driven Design* (2003), §3 Supple Design (seven patterns: Intention-Revealing Interfaces, Side-Effect-Free Functions, Assertions, Conceptual Contours, Standalone Classes, Closure of Operations, Declarative Design) — references/05-domain-driven-design.md
- Ousterhout, *A Philosophy of Software Design* (2018), §1.2 Complexity symptoms (obscurity, unknown unknowns), §3.1 Deep Modules, §3.2 Information Hiding, §4.3 Define Errors Out of Existence — references/01-philosophy-of-software-design.md
- Vernon, *Implementing Domain-Driven Design* (2013), §4.5 Module (when a sub-topic becomes a Bounded Context) — references/04-implementing-ddd.md
