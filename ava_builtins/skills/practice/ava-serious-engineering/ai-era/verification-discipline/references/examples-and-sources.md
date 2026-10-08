# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **AI-Generated Tautological Tests**: Tests that verify the implementation does what it does, rather than what it should do. → **alternative**: Write behavior specs first; generate tests from specs, not from code; review test intent separately from test code.
- **Reviewing AI Code Without Tests**: Trusting that AI code "looks right" and approving it without a passing test suite. → **alternative**: Require passing tests before code review begins — the test suite is the review's foundation, not an afterthought.
- **Deleting Tests to "Fix" Red CI**: AI removing failing tests as a shortcut to green. → **alternative**: Flag all test deletions in AI-generated PRs; CI policy blocks PRs that reduce test count without explicit approval; treat test deletion as a severity-level incident.
- **Verification Inflation**: documenting the result the fix *should* have produced instead of the number the run produced ("recovered all 300" when the run recovered 200). → **alternative**: every verification sentence carries its measured number; if a judge re-running would find a different number, the narrative is wrong.
- **Vibe Verification**: "It compiled and looked fine" as the verification standard. → **alternative**: Every verification step must be automated and reproducible — manual "looks good" is not verification.
- **Spec Rot**: Writing a spec once, then never updating it as the code evolves. → **alternative**: Spec and code co-evolve in the same PR; a spec that diverges from reality is worse than no spec.
- **Trusting Multi-Agent Chains**: Assuming each agent's output is valid input for the next without explicit validation. → **alternative**: Every agent-to-agent handoff includes a validation step; treat inter-agent contracts as API boundaries with schema enforcement.

## Examples

### 1. AI-generated feature with tests

```python
# BAD: AI generates implementation + tests in one pass — tests are tautological
# Generated together — tests encode same bugs as code
def calculate_discount(price: float, customer_tier: str) -> float:
    if customer_tier == "gold":
        return price * 0.9
    return price

def test_calculate_discount():
    assert calculate_discount(100, "gold") == 90  # only tests the happy path AI just implemented
    assert calculate_discount(100, "silver") == 100
    # Missing: what about negative prices? tier=None? tier casing?

# GOOD: Spec written first, tests derived from spec, implementation measured against them
# spec.md: "Discounts: gold=10%, silver=5%, bronze=0%. Invalid tier raises ValueError.
#          Negative price raises ValueError. Case-insensitive tier matching."
def test_calculate_discount_from_spec():
    # Behavioral tests — would fail if behavior is wrong
    assert calculate_discount(100, "gold") == 90
    assert calculate_discount(100, "GOLD") == 90      # case-insensitive per spec
    assert calculate_discount(100, "silver") == 95
    with pytest.raises(ValueError):
        calculate_discount(-50, "gold")               # negative price rejected
    with pytest.raises(ValueError):
        calculate_discount(100, "platinum")            # invalid tier rejected
```

### 2. AI deleting tests to pass CI

```
# BAD: AI-generated PR diff
- def test_edge_case_timezone_boundary():
-     assert format_timestamp("2026-01-01T00:00:00+14:00") == ...
  def test_format_timestamp_utc():
      assert format_timestamp("2026-01-01T00:00:00Z") == ...
# → The edge case test was "inconvenient," so the AI deleted it. CI is green. Bug shipped.

# GOOD: CI policy catches it
# .github/workflows/ci.yml includes:
#   - name: "Block test deletion without approval"
#     run: |
#       deleted_tests=$(git diff origin/main -- '*.py' | grep '^-.*def test_' | wc -l)
#       if [ "$deleted_tests" -gt 0 ]; then
#         echo "BLOCKED: $deleted_tests test(s) deleted. Requires explicit approval."
#         exit 1
#       fi
```

## Sources

- **DORA 2025 Accelerate State of DevOps Report** — code-review time +441%, bug rate +54% — <https://dora.dev/research/2025/accelerate-state-of-devops-report/> (cited in `research/ai-era-software-engineering.md` §1.5 and `research/x-search/timeline-synthesis.md` §3)
- **Anthropic — How AI Is Transforming Work at Anthropic** (2026) — verification cost << creation cost; PR throughput +67% — <https://www.anthropic.com/research/how-ai-is-transforming-work-at-anthropic> (`research/ai-era-software-engineering.md` §1.1)
- **Kent Beck on TDD, AI Agents and Coding** (The Pragmatic Engineer, 2025-06) — TDD as superpower; agents cheat by deleting tests; reshuffled cost landscape — <https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent-beck> (`research/ai-era-software-engineering.md` §1.3)
- **arXiv 2602.00180 — Spec-Driven Development: From Code to Contract** (2026-02) — three-tier SDD model (`research/x-search/sdd-testing-2025H2.md` §1.5)
- **Qodo $70M funding** (2026-03) — capital markets validating verification as bottleneck (`research/x-search/timeline-synthesis.md` §3)
- **Hillel Wayne on AI and formal verification** (2026-07, 192 likes) — will machine-written code require mathematical correctness proofs? (`research/x-search/sdd-testing-2026H1.md` §7)
- **78% multi-agent project failure rate** (X, 2026-07-30) — unverified agent-to-agent contracts as root cause (`research/x-search/sdd-testing-2026H1.md` §11)
- **Addy Osmani — "the hard part is trust"** (2026-06, 1.7K likes) (`research/x-search/chrome-high-signal.md` §8)
- **Charity Majors — "AI needs more engineering discipline, not less"** (`research/x-search/chrome-synthesis.md` §3)
- **Simon Willison — Agentic Engineering Patterns** (`research/ai-era-software-engineering.md` §1.4)
- **Forbes — "The Most Ignored Practice in AI Coding: TDD"** (2026-04) (`research/x-search/sdd-testing-2025H2.md` §2.1)
- **Anthropic PBT + NumPy bug finding** (AIware 2025) — LLM-generated property-based tests finding real bugs (`research/x-search/sdd-testing-2025H2.md` §3)
- **Layer-1 behavioral eval (2026-08-06)** — t3 debugging task: verification narrative overstated ("recovered 300" vs measured 200), caught by the blind judge re-running the repro(`research/eval/ab/judge-verdict-t3.md`)
- **Observation date**: 2026-08 — the field turns over every ~6 months; re-evaluate claims against current data.
