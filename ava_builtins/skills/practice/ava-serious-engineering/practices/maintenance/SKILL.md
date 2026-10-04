---
name: maintenance
description: Evolves existing codebases through broken-window repair, debt tracking, characterization tests, seams, flags, and deletion. Use when fixing rot, modernizing legacy code, paying technical debt, or changing poorly tested existing behavior.
---

# Maintenance

## One-Sentence Core
> Maintenance is not a phase after development — it is development in the presence of accumulated decisions; every unfixed rough edge invites more rough edges, and the only cure is daily governance.

## Core Principles

- **Fix broken windows on sight**:One piece of rotten code left unfixed signals that rot is acceptable — and rot spreads. — **Why**:Thomas & Hunt (Tip 5): the "broken windows" theory applies to code — a TODO left for months, a commented-out block, a hack with no follow-up ticket — each says "we don't care here," and the next developer cares a little less. Complexity accumulates incrementally (Ousterhout §1.4): death by a thousand cuts. — **How**:When you touch a module and see a broken window, fix it in the same change — or, if the fix is too large, log a tracked debt item with a concrete reproduction and link it from the code. Never leave a broken window unacknowledged.

- **Classify technical debt before paying it**:Not all debt is equal — deliberate debt (a known trade-off), accidental debt (the design drifted), and bitrot (the environment changed) need different responses. — **Why**:Martin Fowler's technical-debt quadrant and the wondelai remove-technical-debt skill: treating all debt the same leads to either over-investment (refactoring stable code) or under-investment (ignoring a core-module drift). — **How**:Tag every debt item with type (deliberate/accidental/bitrot), impact (which changes it slows down), and a repayment strategy. Track in a living debt registry (see sweeper skill for the engine). Review the registry weekly — debt that is never discussed is never paid.

- **Legacy code: test first, then change**:You cannot refactor code you cannot verify — characterization tests are the prerequisite for any change to untested legacy code. — **Why**:Feathers (*Working Effectively with Legacy Code*): the definition of legacy code is "code without tests." Without tests, every change is a gamble — you don't know what you broke. — **How**:Before modifying a legacy module, write characterization tests: run the current code with varied inputs, capture the actual output (even if it seems wrong), and assert "the behavior doesn't change." These tests pin the current behavior — they are your safety net. Only then refactor or fix.

- **Use seams and feature flags to cut risk**:A "seam" is a place where you can change behavior without editing the source — dependency injection points, plugin interfaces, configuration switches. Feature flags let you deploy refactored code dark and activate it when proven. — **Why**:Feathers: seams are the only safe way to get legacy code under test. Feature flags (Fowler) turn a risky big-bang cutover into a reversible toggle. Both reduce the blast radius of change. — **How**:Identify seams in the legacy module (where does it get its dependencies? its configuration? its data?). Add a seam if none exists. Introduce the refactored path behind a feature flag; run both paths in production (dark launch); switch the flag when the new path is proven; delete the old path and the flag.

- **Delete dead code — it is not free**:Commented-out blocks, unused parameters, unreachable branches, deprecated endpoints — every line of dead code adds cognitive load, slows down grep, and confuses the next reader. — **Why**:Thomas & Hunt (Tip 14, ETC): dead code makes the system harder to change because it creates false dependencies — a developer reads it, assumes it matters, and codes around it. Version control remembers; the codebase doesn't need to. — **How**:Delete dead code in a dedicated commit with a clear message ("Remove unused X, last caller removed in #1234"). If uncertain, grep for callers; if none, delete. If you feel anxious about deleting, that's a sign the test coverage is insufficient — add tests, then delete.

- **Track debt in a living registry, not in comments**:TODO comments rot — they have no owner, no deadline, no priority, and nobody searches for them. — **Why**:The sweeper skill (Ava): a debt-tracking engine that re-verifies open items and discovers new debt on every sweep. A comment in code is invisible to everyone except the person reading that file; a tracked item in a registry can be queried, prioritized, and assigned. — **How**:Maintain a debt registry (a markdown file or issue tracker) with one line per item: location, type, impact, owner, opened date. Run a sweep weekly: re-verify each item (is it still relevant?), discover new debt (scan recent changes for patterns), and produce a report. The registry is the single source of truth — not scattered TODOs.

## Checklist
- [ ] **MUST** When touching a module and seeing a broken window (TODO, hack, dead code), is it either fixed or tracked?
- [ ] **SHOULD** Does every debt item have a type (deliberate/accidental/bitrot), impact, and repayment strategy?
- [ ] **MUST** Before modifying legacy code, are characterization tests in place that pin current behavior?
- [ ] **MUST** Are risky changes deployed behind a feature flag with a dark-launch period?
- [ ] **SHOULD** Is dead code removed promptly, or tracked for removal with a concrete date?
- [ ] **SHOULD** Is the debt registry reviewed at least weekly?
- [ ] **SHOULD** Are seams (DI points, plugin interfaces) documented so future maintainers know where to cut?
- [ ] **MUST** Is every deprecated path annotated with a migration guide and a removal target version?

## Anti-Patterns
- **Heroic rewrite**:Throwing away the legacy module and rewriting from scratch because "it's ugly." → Rewrites lose accumulated bug fixes and edge-case handling (the code is ugly *because* it handles real-world complexity). Use the strangler fig pattern: replace incrementally behind the same interface.
- **Debt denial**:Refusing to track debt because "we'll get to it later" — later never arrives. → Track every item; the registry makes the cost visible and forces prioritization.
- **Commenting out code instead of deleting it**:"I might need this later." → Version control remembers. Delete it. If you later need it, git has it — and the diff will show exactly what was removed and why.
- **Feature-flag graveyard**:Deploying behind a flag, then never removing the flag or the old path — the codebase now has two implementations forever. → Every feature flag has a removal ticket with a deadline; the flag and the old path are deleted together.
- **Refactoring without tests**:"It's just a small cleanup." → Even a "small cleanup" can change behavior in a way no one notices until production. Characterization tests first, always.

## Examples

### 1. Broken window
```python
# ❌ Bad: broken window left open
def process_order(order):
    # TODO: handle partial fills (jim, 2024-03)
    # This is wrong for multi-warehouse but works for now
    return warehouse.ship_all(order)

# ✅ Good: either fixed or tracked
def process_order(order):
    return warehouse.ship_all(order)

# In debt-registry.md:
# | process_order partial-fill | accidental | multi-warehouse wrong |
#   @core/orders.py:45 | #236 | 2024-03 | repay: add split-ship |
```

### 2. Legacy code without safety net
```python
# ❌ Bad: modifying untested legacy code
def calculate_tax(order):
    # Changed rate from 0.08 to 0.10 — hope nothing breaks!
    return order.subtotal * 0.10

# ✅ Good: characterization test first
def test_calculate_tax_current_behavior():
    """Characterization: pin current behavior before refactor."""
    order = Order(subtotal=100.0)
    assert calculate_tax(order) == 8.0  # current rate is 0.08

# Now change to 0.10 — test fails, you know what you're changing
```

### 3. Feature flag discipline
```python
# ❌ Bad: flag lives forever
if feature_flag('new_checkout_v2'):
    return new_checkout()
return old_checkout()
# (two years later, old_checkout still ships in every binary)

# ✅ Good: flag with removal plan
# DEPRECATED(2026-09): old_checkout removal, see TRACK-421
if feature_flag('new_checkout_v2'):  # TRACK-421: remove by 2026-10
    return new_checkout()
return old_checkout()

# After cutover:
# Commit 1: delete old_checkout()
# Commit 2: delete feature_flag('new_checkout_v2')
```

## Relationships
- `principles/complexity-management` — broken windows are incremental complexity; the debt registry is a complexity ledger
- `principles/conceptual-integrity` — maintenance must preserve the Design Concept; every fix either reinforces or erodes it
- `principles/dependency-management` — seams are dependency-injection points; managing dependencies is half of maintenance
- `practices/testing` — characterization tests are the prerequisite for all legacy-code work
- `practices/review` — review catches broken-window candidates before they land; see review skill for red-flag detection
- `references/03-pragmatic-programmer.md §5, §10.1` — Tips 5–7 (broken windows, stone soup), Tip 65 (refactoring)
- `references/01-philosophy-of-software-design.md §1.4` — incremental complexity accumulation (death by a thousand cuts)
- Feathers, *Working Effectively with Legacy Code* — seams, characterization tests, the definition of legacy code (ecological reference)
- wondelai/skills, `remove-technical-debt` — debt classification and repayment workflow (ecological reference)

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th Anniversary Edition, 2019) — Tips 5–7 (broken windows, stone soup), Tip 14 (ETC, dead code cost), Tip 65 (refactoring)
- Ousterhout, *A Philosophy of Software Design* (2018) — §1.4 (incremental complexity accumulation)
- Feathers, *Working Effectively with Legacy Code* (2004) — characterization tests, seams, the definition of legacy code
- Fowler, Martin — feature flags (strangler fig pattern), technical-debt quadrant
- Ava sweeper skill — debt-tracking engine (living registry, weekly sweep)
- wondelai/skills, `remove-technical-debt` — debt classification and structured repayment workflow
