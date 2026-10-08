# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **Vague Naming**: `process()`, `handle()`, `DataManager` → alternative: name for what the function *does* (`calculateMonthlyRevenue`) and what the class *is* (`Invoice`).
- **Refactoring Without Tests**: diving into a restructuring with no safety net → alternative: ensure the test suite is green; refactor in small steps, running tests after each; if tests do not exist, write characterization tests first.
- **Coincidence Programming**: "it works now, ship it" — without understanding why → alternative: for every changed line, write one sentence explaining why it has the observed effect; if you cannot, investigate until you can.
- **Domain-Oblivious Code**: generic terms (`Entity`, `Item`, `Data`) where domain terms (`Policy`, `Claim`, `Premium`) belong → alternative: build the domain glossary first; use its terms in code; review with a domain expert.
- **Comment Redundancy**: `# increment counter` next to `counter += 1` → alternative: delete restatements; keep only intent, trade-offs, assumptions.
- **Introducing a Second Pattern**: adding a new error-handling style or naming convention when one already exists → alternative: follow the existing pattern; consistency is more valuable than a marginal improvement in one location.

## Examples

### Bad → Good: Naming

**Bad** (vague — forces the reader to open the implementation):
```python
def process(d):
    # ... 40 lines ...
    return result
```

**Good** (signal at the call site):
```python
def calculateOverdueFees(
    account: Account, as_of: Date
) -> Money:
    """Sum of all unpaid invoice fees past their
    grace period as of the given date."""
    # ... implementation ...
```

### Bad → Good: Commenting

**Bad** (restates the code):
```python
# Loop through items
for item in items:
    # If item is active
    if item.status == "active":
        # Add to result
        result.append(item)
```

**Good** (explains what code cannot):
```python
# Only active items are billable. Inactive items
# include cancelled and expired — both have $0
# value and must be excluded from revenue reports.
# See ADR-012 for the billing-cycle assumption.
for item in items:
    if item.status == "active":
        result.append(item)
```

### Bad → Good: Domain Proximity

**Bad** (generic — nobody knows what this does without reading every line):
```python
def process_entity(e: dict) -> dict:
    if e["type"] == 1 and e["status"] == 3:
        e["flag"] = True
    return e
```

**Good** (domain language — an insurance expert can read this):
```python
def markLapsedPolicies(policy: Policy) -> Policy:
    """A policy lapses when premium is unpaid
    30 days past the grace period."""
    if policy.isPastDue( days=30 ):
        policy.markLapsed()
    return policy
```

### Bad → Good: Coincidence Programming

**Bad** (works but nobody knows why):
```python
# Not sure why this fixes the timeout, but it does
time.sleep(0.5)
response = api.fetch()
```

**Good** (understood and explained):
```python
# The upstream API rate-limits to 2 req/s.
# Without this guard we hit 429s under load.
# After we move to a token-bucket limiter (TODO #341),
# this sleep can be removed.
rate_limiter.acquire()
response = api.fetch()
```

## Sources

- Thomas & Hunt, *The Pragmatic Programmer* — naming (Tip 74), refactoring (Tip 65), domain proximity (Tip 22), coincidence programming (Tip 62), DRY and orthogonality (Tips 15–17), "don't outrun your headlights" (Tips 42–43)
- Ousterhout, *A Philosophy of Software Design* — comment philosophy (§5.1), write comments first (§5.2), obviousness and consistency (§5.3), complexity accumulates incrementally — "death by a thousand cuts" (§1.4)
- Evans, *Domain-Driven Design* — ubiquitous language as the bridge between domain and code (ch. 2)
- Fowler, *Refactoring* — bad-smell catalog and the "refactor in small steps with tests" discipline
