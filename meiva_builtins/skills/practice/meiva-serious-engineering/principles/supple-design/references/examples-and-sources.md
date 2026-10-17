# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Evans, *Domain-Driven Design* (2003), §3 Supple Design (seven patterns: Intention-Revealing Interfaces, Side-Effect-Free Functions, Assertions, Conceptual Contours, Standalone Classes, Closure of Operations, Declarative Design) — references/05-domain-driven-design.md
- Ousterhout, *A Philosophy of Software Design* (2018), §1.2 Complexity symptoms (obscurity, unknown unknowns), §3.1 Deep Modules, §3.2 Information Hiding, §4.3 Define Errors Out of Existence — references/01-philosophy-of-software-design.md
- Vernon, *Implementing Domain-Driven Design* (2013), §4.5 Module (when a sub-topic becomes a Bounded Context) — references/04-implementing-ddd.md
