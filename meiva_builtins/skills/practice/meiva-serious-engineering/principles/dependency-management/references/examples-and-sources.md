# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th anniv. ed., 2019), Tips 17 (Orthogonality), 44–47 (Decoupling, Tell Don't Ask, Law of Demeter), 55 (Configuration), 79 (Policy as Metadata) — references/03-pragmatic-programmer.md
- Evans, *Domain-Driven Design* (2003), §2.1 Layered Architecture — references/05-domain-driven-design.md
- Brooks, *The Design of Design* (2010), §3.5 Aesthetics and Style (Orthogonality derived from Consistency) — references/02-design-of-design.md
- Ousterhout, *A Philosophy of Software Design* (2018), §1.2–1.3 Complexity causes, §4.1 Different Layer Different Abstraction, §4.2 Pull Complexity Downwards, §5.3 Consistency — references/01-philosophy-of-software-design.md
