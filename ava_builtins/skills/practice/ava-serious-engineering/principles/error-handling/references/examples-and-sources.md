# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **Defensive null-check sprawl**:Every method starts with `if (x == null) return null;` — null propagates through the call stack, and the eventual error message is "NullPointerException at line 1 of Main" with no hint of the source. → alternative: Crash at the point null first appears where it shouldn't; use `Optional` or `Result` types to make absence explicit and force handling at the call site.
- **Empty catch / log-and-swallow**:`catch (Exception e) { log.error(e); }` — the error is logged and execution continues as if nothing happened, with the system in an unknown state. → alternative: If you can't recover, don't catch. Let it propagate to the layer that can. If you must catch, re-throw or translate into a domain exception.
- **Using exceptions for control flow**:Throwing and catching exceptions for non-exceptional conditions (e.g., using `throw new NotFoundException()` as a "return not found" instead of returning `Optional`). → alternative: Exceptions are for exceptional conditions. Use return types (`Optional`, `Result`, `Either`) for expected alternative outcomes.
- **Catching too broadly**:`catch (Exception e)` at every method boundary — the "I don't know what might go wrong so I'll catch everything" pattern. → alternative: Catch only the specific exception types you can handle. Let unknown exceptions propagate to the top-level handler.
- **Swallowing errors in distributed systems**:A microservice calls another, gets an error, logs it, and returns a partial result. The caller never knows the operation was incomplete. → alternative: Distributed operations must make partial failure explicit — return `PartialSuccess` with a list of what succeeded and what failed, or fail the whole operation with a clear scope.

## Examples

**Example 1: Define Errors Out of Existence**

❌ Bad (exception that could be a normal state):
```python
def get_user(user_id: int) -> User:
    user = db.query("SELECT * FROM users WHERE id = ?", user_id)
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")
    return user

# Every caller must try-catch or propagate
# Control flow is interrupted for a common, expected case
```

✅ Good (absence as normal state):
```python
def get_user(user_id: int) -> Optional[User]:
    return db.query("SELECT * FROM users WHERE id = ?", user_id)

# Caller handles absence on the normal path:
user = get_user(123)
if user is None:
    return Response.not_found()
# No exception, no control-flow interruption
```

**Example 2: Crash Early vs Silent Corruption**

❌ Bad (silent default substitution):
```python
def process_order(order_data: dict) -> Order:
    quantity = order_data.get("quantity", 1)  # silently defaults
    price = order_data.get("price", 0.0)      # silently defaults
    # If the upstream system changed "quantity" to "qty", we ship wrong orders
    # with no error — the bug is discovered by angry customers
    return Order(quantity=quantity, price=price)
```

✅ Good (crash early on contract violation):
```python
def process_order(order_data: dict) -> Order:
    if "quantity" not in order_data:
        raise ValueError("Missing required field: quantity")
    if "price" not in order_data:
        raise ValueError("Missing required field: price")
    quantity = order_data["quantity"]
    price = order_data["price"]
    # Contract violation is caught immediately — the upstream bug is found in CI
    return Order(quantity=quantity, price=price)
```

**Example 3: Error Handling at the Right Layer**

❌ Bad (catching everywhere):
```python
def calculate_total(items: list[Item]) -> float:
    try:
        return sum(item.price for item in items)
    except Exception:
        return 0.0  # what went wrong? unknown. total is silently 0.

def apply_discount(total: float, code: str) -> float:
    try:
        discount = discount_service.lookup(code)
        return total * (1 - discount)
    except Exception:
        return total  # discount silently skipped — the user is overcharged
```

✅ Good (catch at the right layer):
```python
def calculate_total(items: list[Item]) -> float:
    return sum(item.price for item in items)  # no catch — let errors propagate

def apply_discount(total: float, code: str) -> float:
    discount = discount_service.lookup(code)  # no catch — let errors propagate
    return total * (1 - discount)

# Single top-level handler:
@app.route("/checkout")
def checkout():
    try:
        total = calculate_total(cart.items)
        total = apply_discount(total, request.discount_code)
        return Response.ok({"total": total})
    except DiscountServiceError as e:
        return Response.error("DISCOUNT_UNAVAILABLE", str(e))
    except Exception as e:
        logger.exception("Checkout failed")
        return Response.error("INTERNAL_ERROR", "Please try again")
```

## Sources

- Ousterhout, *A Philosophy of Software Design* — Define Errors Out of Existence (§4.3), exception masking, exception aggregation, state-machine self-healing. See `../../../references/01-philosophy-of-software-design.md`.
- Thomas & Hunt, *The Pragmatic Programmer* — Design by Contract (Tip 37), Crash Early (Tip 38), Assertions (Tip 39). See `../../../references/03-pragmatic-programmer.md`.
- Meyer, *Object-Oriented Software Construction* — the original formulation of Design by Contract (preconditions, postconditions, invariants).
