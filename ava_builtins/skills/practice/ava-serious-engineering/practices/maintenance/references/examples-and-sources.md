# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th Anniversary Edition, 2019) — Tips 5–7 (broken windows, stone soup), Tip 14 (ETC, dead code cost), Tip 65 (refactoring)
- Ousterhout, *A Philosophy of Software Design* (2018) — §1.4 (incremental complexity accumulation)
- Feathers, *Working Effectively with Legacy Code* (2004) — characterization tests, seams, the definition of legacy code
- Fowler, Martin — feature flags (strangler fig pattern), technical-debt quadrant
- Ava sweeper skill — debt-tracking engine (living registry, weekly sweep)
- wondelai/skills, `remove-technical-debt` — debt classification and structured repayment workflow
