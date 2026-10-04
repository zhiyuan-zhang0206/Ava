---
name: testing
description: Designs meaningful contract, property, regression, and integration tests while avoiding coverage traps. Use when writing, reviewing, or improving tests, or whenever code is hard to test and its design needs diagnosis.
---

# Testing

## One-Sentence Core
> Testing is a design tool first and a bug detector second — hard-to-test code is poorly designed code, and the unit of testing must be the abstraction, not the feature.

## ⚠️ AI Era Note (2026-08, reconciled with ai-era/guidance.md)

In the AI era the emphasis shifts from "tests drive design" to **"tests are the verification floor"**: AI can write both the code and a test for it, and a test generated alongside the code is a tautology — it encodes the same blind spot. The "design tool" role does not disappear, but it changes shape:

- The test's **first job is now to detect AI cheating** (deleting tests to make code pass, narrowing assertions, testing implementation details) — a test is only worth anything if a human wrote the intent into it.
- The test is still the first consumer of the API — that part is unchanged: hard-to-test code is still poorly designed code, whether a human or an AI wrote it.
- **Write the test yourself, before accepting AI-generated code.** If you cannot write the test, you do not understand the intent — and should not accept the code.

This file's principles below stay valid; read them through the lens above. Cross-reference: `ai-era/guidance.md` (TDD row), `ai-era/verification-discipline`.

## Core Principles

- **Testing is a design tool**:Write tests as the first consumer of your API — the friction you feel is a design smell. — **Why**:Thomas & Hunt (Tip 66): code that is hard to test is code that is hard to use; tests expose coupling, hidden state, and unclear contracts long before production callers do. — **How**:Before writing implementation, write a test that exercises the module's contract. If setup requires five mocks and a global flag, redesign the module — the test is telling you the interface is wrong.

- **Test at the abstraction level, not the feature level**:Unit tests must pin down a module's contract (preconditions, postconditions, invariants), not a feature's happy path. — **Why**:Ousterhout (§7.1) critiques TDD for driving single-feature passes at tiny granularity with no feedback loop for system design; Thomas & Hunt (Tip 37) counter that contracts make tests meaningful. The reconciliation: test the *abstraction* — what the module guarantees — not every code path through a feature. — **How**:For each module, write tests that answer "what must be true after any call to this function?" Use property-based tests (Tip 71) to verify invariants across the state space; complement with targeted unit tests for edge cases found during development.

- **Property-based testing catches what hand-written cases miss**:Define properties (invariants) — for a sort: output is sorted, length equals input length, every input element appears in the output. — **Why**:Hand-written test cases encode the same assumptions as the implementation — the author's blind spot is in both. Properties describe the *shape of correctness* and let the framework search for counterexamples. — **How**:For every module with non-trivial logic, write at least one property: round-trip (encode→decode = identity), idempotence (f(f(x)) = f(x)), commutativity (f(g(x)) = g(f(x))), or domain-specific invariants ("account balance never goes negative").

- **State coverage, not line coverage**:100% line coverage is a vanity metric — the goal is covering meaningful states. — **Why**:Thomas & Hunt (Tip 93): line coverage tells you what executed, not what was tested. A test can hit every line without asserting a single meaningful invariant. — **How**:Identify the module's state space (combination of inputs, internal modes, error conditions). Target coverage of state transitions, not code lines. Use a saboteur (Tip 92): deliberately inject a bug — if no test fails, your coverage is decoration.

- **Regression test after every fix**:A bug that shipped once will ship again unless guarded. — **Why**:Thomas & Hunt (Tip 94): bugs recur because the conditions that caused them — unclear contracts, implicit assumptions, missing edge cases — remain until a test makes them explicit. — **How**:Before fixing a bug, write a test that reproduces it (red). Fix (green). The test stays — it is now a regression guard. One bug, one test, every time.

- **The test pyramid is a design heuristic, not a dogma**:More unit tests (fast, precise), fewer integration tests, fewest end-to-end tests (slow, brittle). — **Why**:Unit tests isolate failures to one module; E2E tests tell you *that* something broke but not *where*. A pyramid inverted (many E2E, few unit) gives slow feedback and vague diagnostics. — **How**:Push behavior down the pyramid: if an E2E test catches a bug, ask "could a unit test have caught this?" and write one. Reserve E2E for smoke tests of the critical user journey.

## Checklist
- [ ] **SHOULD** Can a new developer write a test for this module without reading the implementation?
- [ ] **MUST** Does each module have contract tests (preconditions, postconditions, invariants)?
- [ ] **SHOULD** Are there property-based tests for modules with non-trivial logic?
- [ ] **MUST** Does the test suite survive a saboteur (a deliberately injected bug must cause a failure)?
- [ ] **MUST** Does every bug fix from the last month have a paired regression test?
- [ ] **SHOULD** Is the test pyramid balanced — more unit, less integration, least E2E?
- [ ] **MUST** Are tests independent (no shared mutable state, no order dependency)?
- [ ] **SHOULD** Do tests run fast enough that developers run them before every push?

## Anti-Patterns
- **Coverage worship**:Chasing 100% line coverage with assertions that check nothing ("assert True"). → Replace with state-coverage targets and property-based tests. A lower coverage number with meaningful assertions beats a perfect number with hollow ones.
- **Testing implementation details**:Tests that assert internal variable values or private method call counts break on any refactor, even a correct one. → Test observable behavior through the public contract.
- **E2E-only testing**:A test suite dominated by browser/API tests gives slow feedback and vague failure locations. → Push verification down the pyramid; use E2E only for the critical smoke path.
- **Skipping regression tests**:"I'll add the test later" is the most reliable way to ship the same bug twice. → Write the reproducing test before the fix, every time.
- **Mock-heavy tests**:Tests with five mocks are testing mock behavior, not real behavior. → Redesign for fewer dependencies or use fakes (in-memory implementations) instead of mocks.

## Examples

### 1. Testing behavior vs. testing implementation
```python
# ❌ Bad: tests internal state, breaks on refactor
def test_stack_push():
    s = Stack()
    s.push(1)
    assert s._items == [1]  # internal field!

# ✅ Good: tests observable behavior
def test_stack_push():
    s = Stack()
    s.push(1)
    assert s.peek() == 1
    assert len(s) == 1
```

### 2. Line coverage vs. state coverage
```python
# ❌ Bad: 100% line coverage, zero edge-case coverage
def test_transfer():
    account.transfer(100, to="savings")
    assert account.balance == 900  # happy path only

# ✅ Good: targets states — zero, negative, overflow, concurrent
def test_transfer_insufficient_funds():
    with pytest.raises(InsufficientFunds):
        account.transfer(2000, to="savings")

def test_transfer_idempotent():
    """Property: transferring X then -X leaves balance unchanged."""
    ...
```

### 3. Missing regression test
```python
# ❌ Bad: bug fixed, no guard
# (two weeks later, same bug returns)

# ✅ Good: test written before fix
def test_search_handles_unicode_boundary():
    """Regression: #421 — crash on U+FFFF in search term."""
    result = search("foo￿")
    assert result == []
```

## Relationships
- `principles/complexity-management` — hard-to-test code is a complexity symptom; the testing lens reveals hidden dependencies
- `principles/dependency-management` — testability drives decoupling (dependency injection, pure functions)
- `practices/design` — testability is a design constraint, not an afterthought; design for testability from the start
- `practices/review` — review includes test-coverage assessment; see review skill for the five-axis checklist
- `practices/maintenance` — regression tests are the first line of defense against rot; see maintenance skill for broken-window discipline
- `references/01-philosophy-of-software-design.md §7.1` — TDD critique: the unit of incremental development must be the abstraction
- `references/03-pragmatic-programmer.md §8.3, §10.2` — Tips 66–71 (testing as design tool), Tips 92–94 (saboteur, state coverage, regression)

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th Anniversary Edition, 2019) — Tips 37–39 (design by contract), Tips 66–71 (testing as design tool), Tips 92–94 (state coverage, saboteur, regression)
- Ousterhout, *A Philosophy of Software Design* (2018) — §7.1 (TDD critique: abstraction-level testing)
- addyosmani/agent-skills, `test-driven-development` — red-green-refactor workflow with exit criteria (ecological reference)
