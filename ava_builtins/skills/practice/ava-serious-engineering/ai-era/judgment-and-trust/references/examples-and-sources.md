# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **Blind Trust**:Accepting AI-generated code because "it looks right" or "it passes tests" without understanding its reasoning. → **Alternative**:Require that you can articulate the *why* behind every non-trivial design choice before merging. "It works" is not a substitute for "I understand."
- **Rubber-Stamp Review**:Treating code review of AI-generated code as a formality because "the AI wrote it" or "the tests pass." → **Alternative**:Apply stricter scrutiny to AI-generated code than to human-written code — human engineers at least have reputational skin in the game; AI has none.
- **Architecture Drift by Accumulation**:Accepting small architectural inconsistencies in each AI-generated change because each one seems "minor." → **Alternative**:Treat architecture consistency as a non-negotiable invariant. Each accepted drift is a loan against future understanding — the interest compounds.
- **Experience Bypass**:Delegating judgment calls to junior engineers or AI agents without an experienced gatekeeper in the loop. → **Alternative**:The most experienced engineer on the team should review the most AI-generated code — experience is the multiplier that turns AI from a risk into a lever.

## Examples

### Bad → Good: Trusting AI-Generated Logic

**Bad (blind trust)**:
```
# AI generates this; reviewer approves because "tests pass"
def calculate_discount(order_total, customer_tier):
    if customer_tier == "premium":
        return order_total * 0.15
    elif customer_tier == "gold":
        return order_total * 0.10
    return 0  # ← subtle: new "basic" tier gets 0, but legacy "none" also gets 0
```
**Good (verified trust)**:
```
# Before merging, engineer asks: "What happens for legacy customers with tier=None?"
# AI-generated code is amended with explicit intent:
def calculate_discount(order_total, customer_tier):
    """Apply tier-based discount. Customers with no tier or unrecognized tier receive no discount.

    Invariant: discount never exceeds 20% of order_total.
    See: ADR-014 for tier definitions.
    """
    DISCOUNT_MAP = {"premium": 0.15, "gold": 0.10}
    discount_rate = DISCOUNT_MAP.get(customer_tier, 0.0)
    return order_total * discount_rate
```

### Bad → Good: Architecture Drift by Accumulation

**Bad (accepting inconsistency)**:
- Sprint 1: AI adds a `send_notification()` helper in `utils/` (new pattern)
- Sprint 3: AI adds another notification helper in `services/notify.py` (different pattern)
- Sprint 5: AI adds a third in `api/middleware.py` (yet another pattern)
- Sprint 8: Three different notification mechanisms exist; no one knows which to use.

**Good (gatekeeper enforces consistency)**:
- Sprint 1: AI proposes `send_notification()` in `utils/`. Gatekeeper: "We have `services/messaging.py` for all outbound communication. Move it there and follow the existing `MessageBus` pattern."
- Sprint 3: AI references `MessageBus` correctly because the codebase's pattern is explicit and enforced.
- Result: Architecture remains coherent, AI has clear precedents to follow, new engineers (human or AI) can discover the pattern.

## Sources

- **X/Twitter Research (2025H2–2026H1)**:arch-quality-2026H1 (bottleneck shift, AI entropy, gatekeeper role); econ-2026H1 (bottleneck economics, ROI debate); chrome-high-signal (Addy Osmani "hard part = trust," Replit 3× output data); chrome-key-people (Kent Beck amplifier/equalizer, code review model dead, flow state loss)
- **Synthesis Reports**:synthesis.md §1–§10 (10 change points with evidence); timeline-synthesis.md (four-phase evolution of the bottleneck narrative)
- **DORA 2025**:AI Productivity Paradox — individual output +98%, review time +441%, bugs +54% — <https://dora.dev/research/2025/accelerate-state-of-devops-report/>
- **GitClear (211M changed lines, 2025 report)**:copy/paste 8.3% → 12.3% (2020→2024, +48% relative; cloning rate 4× year-over-year), moved code 25% → <10% — <https://www.gitclear.com/ai_assistant_code_quality_2025_research>
- **Anthropic Internal Research**:PR merge rate +67%, experienced engineers extract more value
- **Addy Osmani**:"The hard part of engineering moved from writing code to deciding whether to trust it" (June 2026)
- **Kent Beck**:"Expected a compressor, got an amplifier" (July 2026); "Code review made sense when humans wrote code at human speed. The old model broke" (December 2025)
- **ai-era-software-engineering.md**:Comprehensive survey of 40+ resources across six themes
