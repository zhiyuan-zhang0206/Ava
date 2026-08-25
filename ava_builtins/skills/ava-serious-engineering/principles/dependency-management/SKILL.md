---
name: dependency-management
description: Audits coupling and points dependencies toward stable, explicit boundaries. Use when designing an API, choosing dependency direction, untangling modules that break together, or deciding whether infrastructure is leaking into domain logic.
---

# Dependency Management

## One-Sentence Core
> Every dependency is a future change amplified — manage them by pointing dependencies toward stability, keeping orthogonal concerns independent, and making every coupling explicit, minimal, and one-way.

## Core Principles

- **Dependencies Point Inward, Toward Stability**: higher-level policy and domain logic must never depend on lower-level infrastructure, frameworks, or UI details — the dependency arrow always points from concrete to abstract, from volatile to stable. **Why**: Evans' layered architecture (05 §2.1) established the foundational rule: the Domain layer is the core and must not depend on Infrastructure or UI; any dependency reversal (domain importing a database library) makes the core fragile — a database migration forces domain logic changes. Ousterhout (01 §4.1) generalized this as "different layer, different abstraction": when adjacent layers share similar abstractions, it signals pass-through methods that add interface cost without hiding complexity. **How**: define interfaces (Ports) in the domain/application layer; implement them in the infrastructure layer; the domain never imports a framework, a database driver, or an HTTP client directly — it only imports the interface it owns.

- **Orthogonality Is the Master Test**: two concerns are orthogonal if changing one does not force a change in the other — the single most powerful design heuristic, and its absence is the single most reliable signal of architectural decay. **Why**: Thomas & Hunt (Tip 17, 03 §3.1) defined orthogonality as the operational test "when you change A, must you also change B?" — a non-orthogonal system amplifies every change (Ousterhout's "change amplification," 01 §1.2) and eventually becomes frozen because every modification risks breaking something unrelated. Brooks (02 §3.5) derived orthogonality from the deeper principle of Consistency: knowing part lets you predict the rest — orthogonal features don't interfere, so predicting behavior does not require simulating interactions. **How**: for every module pair, ask the orthogonality question during design review; if two modules always change together, consider merging them; if changing module A's internal data format forces module B to recompile, the format is a leaked dependency — encapsulate it behind an interface; use the "one reason to change" test: a module that changes for both business-rule updates and database-schema migrations is not orthogonal.

- **Tell, Don't Ask; Obey the Law of Demeter**: callers should tell an object what to do, not interrogate its internal state and make decisions on its behalf; a method should only talk to its immediate neighbors — not traverse an object graph. **Why**: Thomas & Hunt Tips 45–46 (03 §6.1) identified the chain `obj.getX().getY().doSomething()` as the archetypal coupling pattern: the caller knows the internal structure of `obj` (it has an X, which has a Y), so changing `obj`'s internal design breaks every caller that traverses it. This is not a style rule — it is a coupling rule: every dot in a method chain is a dependency that will amplify future changes. **How**: instead of `order.getCustomer().getAddress().getZipCode()`, give `Order` a `getDeliveryZipCode()` method that encapsulates the traversal; instead of `if (account.getBalance() > threshold) account.setStatus(Overdrawn)`, give `Account` a `assessOverdraft(threshold)` method. Count dots in method chains during code review — more than two dots on objects of different types is a Demeter violation to investigate.

- **Dependencies Must Be Injected, Not Discovered**: a module that reaches out to find its dependencies (service locator, global registry, `import` of a concrete implementation) hides its coupling and makes it impossible to reason about from the interface alone. **Why**: Ousterhout (01 §1.2) classified hidden dependencies under "obscurity" — the second root cause of complexity — because a module's interface does not tell you what it actually needs. A class that calls `ServiceLocator.get<PaymentGateway>()` internally has a dependency on PaymentGateway that is invisible from its constructor signature, making it impossible to test in isolation and impossible to audit without reading the implementation. **How**: pass every dependency through the constructor or method parameter; the interface is the contract — if a class needs a `PaymentGateway`, the constructor says so; if a function needs a `Logger`, the parameter list says so. This makes dependencies greppable, testable, and explicit — Ousterhout's "obviousness" principle (01 §5.3) applied to coupling.

- **Configuration Is Externalized; Policy Is Not Hardcoded**: any value that varies between deployments (environment, scale, region) or that a business stakeholder might want to change without a code deploy must live outside the code; business rules that are data must be expressed as data. **Why**: Thomas & Hunt Tip 55 (03 §6.4) established that config and code must be separate because their change cadences differ — config changes with the environment, code changes with the requirements. Tip 79 (03 §9) added that policy ("VIP quota is 10×") expressed as hardcoded if-else is a change-amplification trap: a policy change should be a data update, not a code deploy. **How**: environment-specific values go in env vars or config files, never in constants; business policy thresholds go in a configuration store that can be updated without redeploying; but don't over-configurify — a parameter that has never changed and has no foreseeable reason to change is not config, it is complexity masquerading as flexibility (Ousterhout 01 §4.2: "configuration-parameter abuse").

## Checklist
- [ ] **MUST** Does the domain logic import any infrastructure, framework, or database library directly?
- [ ] **MUST** For any pair of modules A and B: if A's internal data format changes, must B be modified?
- [ ] **SHOULD** Are there any method chains longer than two dots on objects of different types (Law of Demeter check)?
- [ ] **SHOULD** Does every class declare its dependencies in its constructor rather than discovering them internally?
- [ ] **MUST** Are all deploy-environment values in configuration files, not hardcoded constants?
- [ ] **SHOULD** Are business policy thresholds expressed as configurable data, not if-else branches?
- [ ] **MUST** Do higher-level modules depend on interfaces defined in their own layer, not on concrete lower-level implementations?
- [ ] **SHOULD** Can each module be tested in isolation with its dependencies replaced by test doubles?
- [ ] **MUST** When a single requirement change touches more than 2–3 files, is the coupling pattern identified and addressed?

## Anti-Patterns
- **Inverted Dependency**: the domain model imports a database ORM, an HTTP client, or a framework class — infrastructure changes break business logic. → **Alternative**: define a repository interface in the domain layer; implement it in infrastructure; the domain only knows the interface.
- **Train Wreck**: `order.getCustomer().getAddress().getCity().toUpperCase()` — the caller knows the entire object graph structure. → **Alternative**: encapsulate the traversal behind a single method on the root object: `order.getDeliveryCity()`.
- **Service Locator**: code calls `Container.resolve<IPaymentGateway>()` in the middle of a method — the dependency is invisible from the constructor. → **Alternative**: require `IPaymentGateway` as a constructor parameter; let the DI container wire it at startup, not at every call site.
- **Global Mutable State**: a singleton `CurrentUser` or `RequestContext` that every module reads and writes — any module can break any other module through the global. → **Alternative**: pass state explicitly through method parameters or a context object scoped to the request lifetime.
- **Over-Configurification**: 200 config knobs, most of which have never been changed from their defaults and none of which anyone understands. → **Alternative**: start with zero config; add a knob only when a real deployment scenario demands a different value; document each knob's purpose and default.

## Examples

**Bad (Tell, Don't Ask violation)**:
```python
if account.balance < minimum_balance:
    account.status = "overdrawn"
    notification_service.send(account.owner, "Your account is overdrawn")
```
The caller interrogates `account.balance`, makes a decision, and mutates `account.status` externally — the overdraft rule is scattered across callers.

**Good**:
```python
account.assess_overdraft(minimum_balance)
```
The `Account` class encapsulates the overdraft rule. It checks its own balance, updates its own status, and raises a `AccountOverdrafted` domain event. Callers only tell the account to assess itself — they do not need to know how.

**Bad (Inverted dependency)**:
```python
# domain/order_service.py
from sqlalchemy import select
from infrastructure.database import session

def place_order(items):
    order = Order(items)
    session.add(order)  # Domain depends on SQLAlchemy session
    session.commit()
```

**Good**:
```python
# domain/order_service.py
class OrderService:
    def __init__(self, order_repository: OrderRepository):
        self.order_repository = order_repository

    def place_order(self, items):
        order = Order(items)
        self.order_repository.save(order)

# domain/order_repository.py (interface / Port)
class OrderRepository(ABC):
    @abstractmethod
    def save(self, order: Order) -> None: ...

# infrastructure/sql_order_repository.py (Adapter)
class SqlOrderRepository(OrderRepository):
    def save(self, order: Order) -> None:
        session.add(order)
        session.commit()
```

**Bad (Non-orthogonal modules)**: changing the HTTP response format from XML to JSON requires editing `PaymentController`, `InvoiceController`, and `ReportGenerator` — three modules each contain their own serialization logic. The serialization concern is not orthogonal to the business logic.

**Good**: a single `ResponseSerializer` module owns the serialization concern. Changing the format touches one file. Each controller delegates to the serializer without knowing the format.

## Relationships
- **principles/complexity-management**: dependency and obscurity are the two root causes of complexity (Ousterhout 01 §1.3); dependency management directly attacks the first.
- **principles/bounded-context**: the Context Map relationship types are dependency-management decisions at the system level; ACL is dependency inversion applied to external systems.
- **principles/conceptual-integrity**: Brooks' orthogonality (derived from Consistency, 02 §3.5) is the design-level test; dependency management is the code-level enforcement.
- **principles/supple-design**: standalone classes and operation closure reduce dependencies at the class level.
- **practices/testing**: testability is the canary — a module that is hard to test has hidden dependencies.
- **references/03-pragmatic-programmer.md**: Tips 44–47 (Decoupling, Tell Don't Ask, Law of Demeter, Global Data), Tip 17 (Orthogonality), Tip 55 (Configuration).
- **references/05-domain-driven-design.md §2.1**: Layered Architecture dependency direction.
- **references/02-design-of-design.md §3.5**: Orthogonality derived from Consistency.
- **references/01-philosophy-of-software-design.md §1.2–1.3**: Dependencies as a root cause of complexity; §4.1 Different Layer, Different Abstraction; §4.2 Pull Complexity Downwards.

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th anniv. ed., 2019), Tips 17 (Orthogonality), 44–47 (Decoupling, Tell Don't Ask, Law of Demeter), 55 (Configuration), 79 (Policy as Metadata) — references/03-pragmatic-programmer.md
- Evans, *Domain-Driven Design* (2003), §2.1 Layered Architecture — references/05-domain-driven-design.md
- Brooks, *The Design of Design* (2010), §3.5 Aesthetics and Style (Orthogonality derived from Consistency) — references/02-design-of-design.md
- Ousterhout, *A Philosophy of Software Design* (2018), §1.2–1.3 Complexity causes, §4.1 Different Layer Different Abstraction, §4.2 Pull Complexity Downwards, §5.3 Consistency — references/01-philosophy-of-software-design.md
