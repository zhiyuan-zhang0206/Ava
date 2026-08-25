---
name: implementation
description: Produces obvious, domain-aligned implementation code through precise naming, safe refactoring, and intentional comments. Use when writing or refactoring code, even if tests pass and the requested change appears mechanical.
---

# Implementation Practice

## One-Sentence Core
> Implementation is not typing code that passes tests — it is the discipline of expressing the domain clearly, keeping every change safe, and leaving the code more obvious than you found it.

## Core Principles

- **Name for Signal, Not for Noise**: a name must tell the reader what the thing *does*, not that it *processes something* — **Why**: names are the most frequently read text in a codebase; a vague name like `process()` forces every reader to open the implementation, multiplying cognitive load across the team; a precise name like `calculateMonthlyRevenue()` answers the question at the call site (Thomas & Hunt, Tip 74). — **How**: after writing a function or variable, read its name in isolation and ask "would a colleague know what this is without opening the body?" If not, rename it. Ban the words `process`, `handle`, `data`, `info`, `manager`, `util` unless they carry specific domain meaning.

- **Refactor with a Safety Net**: never refactor without tests — and refactor early, a little at a time, not in a dedicated week when rot is already deep — **Why**: refactoring without tests is gambling; tests are the only proof that behavior is preserved. Small, frequent refactoring prevents the "death by a thousand cuts" accumulation of complexity (Ousterhout §1.4); a dedicated refactoring week signals the codebase has already decayed past the point of easy recovery (Thomas & Hunt, Tip 65). — **How**: before every refactoring step, ensure the test suite is green. Make one small change, run tests, commit. If a change breaks something unexpected, revert and understand why before proceeding. A refactoring step that touches more than 3–4 files at once is too large.

- **Program Close to the Problem Domain**: the domain's own concepts — accounts, invoices, routes, candidates — must be first-class in code — **Why**: code that uses the domain's language is readable by domain experts, not only by engineers; a sea of `process_data_v2` and `handle_request` means the implementation has drifted away from the problem it solves, and every change requires translation between "what the business needs" and "what the code says" (Thomas & Hunt, Tip 22; Evans, Ubiquitous Language). — **How**: when writing a new module, first list the 5–10 key nouns and verbs from the domain. Use those exact words in class names, function names, and variable names. If the domain expert would not recognize a term in your code, replace it.

- **Don't Program by Coincidence**: after something works, state precisely *why* — "this line changed X to Y, so Z no longer happens" — **Why**: code that works by accident is debt you cannot debug; when it breaks later (and it will), nobody knows which change was the real fix and which was coincidence. Understanding every line you ship is the difference between engineering and trial-and-error (Thomas & Hunt, Tip 62). — **How**: before committing, for every changed line, write one sentence in the commit message explaining *why* it has the effect it does. If you cannot explain a line, do not ship it — investigate until you can.

- **Comments Explain What Code Cannot**: comments should describe intent, trade-offs, assumptions, and invariants — not restate the code — **Why**: code can precisely describe *how* but can never capture *why* the design chose this path, what alternatives were rejected, or what assumptions the implementation depends on. A comment that says `# increment i` next to `i += 1` is noise; a comment that says `# Use a stable sort — downstream code depends on equal-key ordering` is signal that prevents a future bug (Ousterhout, §5.1). — **How**: after writing a function, read its comments and delete every line that merely restates the code. What remains should be intent, trade-offs, assumptions, and cross-module constraints. If nothing remains, the function may be obvious enough — or you may be missing design rationale that needs recording.

- **Make the Code Obvious**: a reader should be able to guess the macro behavior of a module without reading every line — **Why**: obvious code reduces cognitive load for every future reader; non-obvious code forces everyone to reverse-engineer intent from implementation, multiplying the cost of every change (Ousterhout, §5.3). Consistency is the cheapest path to obviousness: when knowing one part lets you predict the rest, the system becomes learnable. — **How**: reject semantically vague generic containers like `Pair`, `Tuple`, or `Map<String, Object>` — use named data classes or typed dictionaries. Follow the same calling convention, error-handling pattern, and naming scheme across the codebase. When a pattern already exists, use it; do not introduce a second way to do the same thing.

## Checklist

- [ ] **SHOULD** Does every function and variable name signal its purpose (no `process`, `handle`, `data`, `info` without domain meaning)?
- [ ] **MUST** Are tests passing before every refactoring step — and does each step touch at most 3–4 files?
- [ ] **MUST** Can a domain expert (non-engineer) recognize their business concepts in the code?
- [ ] **MUST** For every line you are about to commit: can you explain *why* it works, not just that it works?
- [ ] **SHOULD** Do comments describe intent, trade-offs, and assumptions — and not restate what the code already says?
- [ ] **SHOULD** Can a newcomer guess the macro behavior of each module without reading the implementation line-by-line?
- [ ] **SHOULD** Are bad smells (duplication, long method, feature envy, shotgun surgery, primitive obsession) flagged for cleanup?
- [ ] **MUST** When a pattern already exists in the codebase, is it followed — or is a second way being introduced?

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

## Relationships

- **Principles**: `principles/complexity-management` — every refactoring step should reduce complexity; bad smells are the implementation-level symptoms of the complexity formula (change amplification = shotgun surgery, cognitive load = long method, unknown unknowns = conjoined methods). `principles/ubiquitous-language` — naming and domain proximity are the implementation-side enforcement of ubiquitous language.
- **Practices**: `practices/design` — the interface comments and ADRs written during design become the contracts implementation must honor; `practices/testing` — tests are the safety net that makes refactoring safe; `practices/review` — bad-smell identification is the core of code review.
- **References**: `references/01-philosophy-of-software-design.md` §5.1–5.3 (comments, obviousness, consistency), §1.4 (complexity accumulates incrementally); `references/03-pragmatic-programmer.md` Tips 22, 62, 65, 74.

## Sources

- Thomas & Hunt, *The Pragmatic Programmer* — naming (Tip 74), refactoring (Tip 65), domain proximity (Tip 22), coincidence programming (Tip 62), DRY and orthogonality (Tips 15–17), "don't outrun your headlights" (Tips 42–43)
- Ousterhout, *A Philosophy of Software Design* — comment philosophy (§5.1), write comments first (§5.2), obviousness and consistency (§5.3), complexity accumulates incrementally — "death by a thousand cuts" (§1.4)
- Evans, *Domain-Driven Design* — ubiquitous language as the bridge between domain and code (ch. 2)
- Fowler, *Refactoring* — bad-smell catalog and the "refactor in small steps with tests" discipline
