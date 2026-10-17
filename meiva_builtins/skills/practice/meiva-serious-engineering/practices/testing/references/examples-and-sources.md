# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th Anniversary Edition, 2019) — Tips 37–39 (design by contract), Tips 66–71 (testing as design tool), Tips 92–94 (state coverage, saboteur, regression)
- Ousterhout, *A Philosophy of Software Design* (2018) — §7.1 (TDD critique: abstraction-level testing)
- addyosmani/agent-skills, `test-driven-development` — red-green-refactor workflow with exit criteria (ecological reference)
