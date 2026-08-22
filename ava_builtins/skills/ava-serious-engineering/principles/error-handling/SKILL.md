---
name: error-handling
description: "Use when error handling is done by vibes — scattered try-catch blocks, inconsistent error recovery, defensive null checks everywhere, or errors that propagate too far and crash the wrong layer. Error handling is one of the worst sources of complexity; the best error is the one you never need to handle."
---

# Error Handling

## One-Sentence Core
> The most effective way to handle errors is to define them out of existence — redesign semantics so that error states become normal states. When errors are unavoidable, crash early, use contracts to make expectations explicit, and handle errors at the right layer.

## Core Principles

- **Define errors out of existence — the best error is no error at all**:Redesign API semantics so that what was an exceptional condition becomes a normal, well-defined behavior. — **Why**:Error handling is one of the worst sources of complexity. In distributed systems, over 90% of catastrophic failures come from incorrect error handling, not from the errors themselves. Every exception path is a branch that must be tested, documented, and maintained — and is often untested and wrong. Eliminating the exception class entirely eliminates all of that burden. — **How**:For every exception your API can throw, ask: "Could I redesign the semantics so this is not an exception?" Classic examples: Unix deleting an open file (succeeds immediately, marks deleted, frees on close — eliminates Windows' "file in use" exception); Python slices silently clamping out-of-range (eliminates Java substring's defensive bounds-check burden); returning an empty collection instead of throwing `NotFound`.

- **Design by Contract: make expectations explicit**:Every function defines preconditions (what the caller guarantees), postconditions (what the function guarantees back), and invariants (what remains true throughout). — **Why**:Thomas & Hunt's Tip 37: when a caller violates the contract, the function should refuse immediately — not struggle through with a default or a guess. Implicit expectations become folklore; explicit contracts become enforceable. — **How**:For every public function, write the contract in its interface comment: (1) Preconditions — what must be true on entry? (2) Postconditions — what will be true on exit? (3) Invariants — what stays true across calls? Use the type system to enforce what it can (e.g., `NonNegativeInt` instead of `int` with a comment); use assertions for the rest. When a precondition is violated, crash — do not guess.

- **Crash early — don't let errors propagate and corrupt state**:Report errors the moment they are detected. The further an error propagates from its source, the harder it is to diagnose and the more likely it is to corrupt state along the way. — **Why**:Thomas & Hunt's Tip 38: a tool returns schema-violating data, you silently stuff a default; half an hour later behavior is weird and you debug forever to find the upstream pollution. A dead program tells you exactly where the problem is; a program that limps along with corrupted state tells you nothing. — **How**:At every module boundary, validate inputs against the contract. If they fail, crash immediately with a clear message stating what was expected and what was received. Do not substitute defaults, do not log-and-continue, do not return `null` to be checked later. The only exception is at the outermost system boundary (HTTP handler, message queue consumer), where crashing means losing availability — there, catch, log, and return a controlled error response.

- **Assertions guard the impossible — and the impossible happens**:Use assertions to encode invariants that must always hold. Assertions are not error handling for expected conditions; they are the declaration that "this condition being false means the program is broken." — **Why**:Thomas & Hunt's Tip 39: that "impossible" branch — it will happen. Assertions serve two purposes: they catch bugs early (during development and testing, never in production where they'd crash users), and they document invariants that would otherwise be implicit. Don't strip assertions in production builds unless performance profiling proves they are the bottleneck. — **How**:Use assertions for: (a) invariants at function entry/exit; (b) default branches in exhaustive switches that should never be reached; (c) state machine transitions that should be impossible given the current state; (d) results of private methods that callers depend on. Do NOT use assertions for: user input validation, network errors, file-not-found — those are expected conditions, not program bugs.

- **Handle errors at the right layer — not everywhere**:Don't catch exceptions in every small method. Let exceptions propagate to the layer that knows what to do about them. — **Why**:Scattered try-catch blocks create three problems: (1) they obscure the normal control flow; (2) they encourage inconsistent recovery strategies; (3) they often swallow errors silently (empty catch blocks) because the local method has no meaningful recovery to offer. — **How**:Use three techniques from Ousterhout: (1) Exception masking — low layers catch and silently handle transient errors (internal retry with backoff on rate limits) so high-level logic never sees them. (2) Exception aggregation — let exceptions propagate to a single top-level handler with one global fallback policy, rather than catching in every method. (3) State-machine self-healing — define "data not found" as a normal state (`Optional`, empty collection) rather than an exception, so callers handle it on the normal path.

- **Combine crash-early with graceful degradation at the system boundary**:The principle splits by layer. Internal module boundaries: crash early — the caller is code you control, and a contract violation means a bug. External system boundaries: degrade gracefully — the caller is the outside world, and crashing means lost availability. — **Why**:Internal crashes surface bugs immediately and precisely; a crash with a clear contract-violation message is the most debuggable failure mode. External crashes lose user trust and availability; the system boundary must convert contract violations into controlled error responses. — **How**:At the outermost HTTP handler, message consumer, or CLI entry point: validate all external input, catch all unhandled exceptions, log the full context, and return a structured error response. Inside the system: trust the contract; crash on violation.

## Checklist

- [ ] **SHOULD** For every exception type the system can throw: could the API semantics be redesigned to make this a normal state instead? (Start with the top 3 most frequent exceptions.)
- [ ] **MUST** Does every public function document its contract — preconditions, postconditions, invariants — either in the type system or in interface comments?
- [ ] **MUST** When a precondition is violated by an internal caller, does the function crash immediately with a clear message (not substitute a default, not return null)?
- [ ] **MUST** Are assertions used for invariants (program bugs), not for expected conditions (user input, network errors, file-not-found)?
- [ ] **MUST** Are there empty catch blocks or `catch (Exception)` anywhere in the codebase? Each one is a potential swallowed error.
- [ ] **MUST** Do transient errors (rate limits, network timeouts) get handled at the low layer (retry, backoff) rather than propagated to business logic?
- [ ] **MUST** Is there a single top-level error handler for each system boundary (HTTP, message queue, CLI) that converts all unhandled exceptions into controlled error responses?
- [ ] **MUST** For distributed operations: are partial failures handled explicitly, or does the system silently continue with incomplete state?

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

## Relationships

- `principles/complexity-management` — Define Errors Out of Existence is a direct application of "pull complexity down": eliminate exception classes by redesigning semantics. Error handling is one of the worst sources of complexity (§4.3 of Ousterhout).
- `principles/conceptual-integrity` — Error handling conventions must be consistent across the system: the same error class should produce the same response shape everywhere. Inconsistency in error handling is a conceptual-integrity defect.
- `principles/bounded-context` — Error semantics may differ across Bounded Contexts: a "not found" in the Catalog context may be a 404, while in the Inventory context it may be a 200 with `stock: 0`. The context boundary defines which error semantics apply.
- `practices/testing` — Property-based testing (03 Tip 71) is especially effective for error handling: define the property "for any invalid input, the system returns a controlled error, not a crash or silent corruption."
- `practices/implementation` — Design by Contract translates directly into implementation: preconditions become assertions or type constraints at function entry; postconditions become assertions or tests at function exit.
- `references/01-philosophy-of-software-design.md` §4.3 — Define Errors Out of Existence and the three companion techniques (exception masking, aggregation, state-machine self-healing).
- `references/03-pragmatic-programmer.md` Tips 37–39 — Design by Contract, Crash Early, and Assertions.

## Sources

- Ousterhout, *A Philosophy of Software Design* — Define Errors Out of Existence (§4.3), exception masking, exception aggregation, state-machine self-healing. See `references/01-philosophy-of-software-design.md`.
- Thomas & Hunt, *The Pragmatic Programmer* — Design by Contract (Tip 37), Crash Early (Tip 38), Assertions (Tip 39). See `references/03-pragmatic-programmer.md`.
- Meyer, *Object-Oriented Software Construction* — the original formulation of Design by Contract (preconditions, postconditions, invariants).
