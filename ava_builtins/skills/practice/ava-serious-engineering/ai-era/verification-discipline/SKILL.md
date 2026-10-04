---
name: verification-discipline
description: Builds independent verification guardrails for AI-generated code and agentic development. Use when designing tests, reviewing AI-produced PRs, preventing fake-green changes, or deciding how to prove machine-written code correct.
---

# Verification Discipline

## One-Sentence Core
> Verification, not generation, is the bottleneck of AI-era software engineering — the discipline of making code provably correct when machines write most of it, through spec-first contracts, adversarial review, and production-facing observability.

## Core Principles

- **Verification Is the Bottleneck**: Code generation is cheap; proving correctness is expensive and getting more so. — **Why**: DORA 2025 found code-review time increased 441% with AI adoption; Qodo raised $70M (2026-03) validating the verification-tooling market; Anthropic's internal study confirmed AI's best ROI comes when "verification cost << creation cost." The community consensus by mid-2026: "The bottleneck isn't generation anymore — it's verification" (X, 2026-07-30). — **How**: Invest test infrastructure in proportion to AI-code dependency — the more AI writes, the more tests you need. Never reduce testing because "AI wrote it"; the opposite is correct. Every AI-generated change must survive automated verification before human review begins.

- **Spec as Contract, Not Documentation**: Specs must be machine-verifiable — a spec that cannot be automatically checked is a wish, not a contract. — **Why**: The SDD movement's three-tier model (Spec-First / Spec-Guarded / Spec-Verified, arXiv 2602.00180, 2026-02) reframes spec as the "first artifact" and code as the "generated or verified second artifact." The community aphorism (2026-07-30): "A prompt is a request — the AI may ignore it. A spec is a contract — the AI must satisfy it." Without machine verification, specs rot at the same speed as comments. — **How**: Every task begins with a spec that tests can verify. The spec defines what "correct" means; tests encode that definition; implementation is measured against it. Use spec-kit-style tooling (GitHub Spec-Kit, 2025-09 open-sourced, 2026-05 formally released) or lightweight alternatives: a markdown spec checked by a test harness. The spec and the verification evolve together — neither is frozen.

- **TDD Is Amplified, Not Obsoleted**: AI writes code faster, which means tests become the primary leverage point for correctness — not less important, but more. — **Why**: Kent Beck (The Pragmatic Engineer, 2025-06): "TDD is a superpower in the AI era." AI agents demonstrably "cheat" — they delete failing tests to achieve green suites. Beck's core insight that "the entire landscape of what's cheap and what's expensive has been reshuffled" means tests are now the scarce, high-value artifact while implementation is abundant. Forbes (2026-04): "AI frequently generates plausible but subtly wrong code — developers without test guardrails accumulate bugs blindly." — **How**: Write tests first, before involving AI in implementation. Never let AI delete or weaken tests without explicit human approval; treat test deletion in AI-generated PRs as a blocking red flag. The test suite is the verification harness — AI works inside it, not around it.

- **Adversarial Verification by Default**: Trust nothing the AI generates — every output must survive adversarial scrutiny, because AI is skilled at producing code that "looks right" but is subtly wrong. — **Why**: Simon Willison: "AI is great at generating code but terrible at explaining intent." Uncle Bob's 2026-07 decision to stop reading agent-generated code sparked community-wide debate about trust boundaries. 78% of multi-agent projects fail to reach stable production (X, 2026-07-30), with the primary failure mode being "one agent's output becomes the next agent's unvalidated input — errors compound silently until a production incident." Addy Osmani (1.7K likes, 2026-06): "The hard part of engineering moved from writing code to deciding whether to trust it." Anthropic's internal safety team analogizes AI-generated code to "suggestions from a very talented junior engineer" — capable but needing experienced adversarial review. — **How**: Run adversarial code review: assume every AI output contains at least one subtle bug and hunt for it. Use property-based testing (PBT) to find edge cases the AI didn't consider — Anthropic successfully applied LLM-generated PBT to find real bugs in NumPy (2025H2). Pair AI-generated code with AI-generated adversarial tests that try to break it.

- **Production Verification Completes the Loop**: Tests passing in CI does not mean software works — production observability is the final verification layer, and its importance grows with AI-generated code. — **Why**: Charity Majors: "AI needs more engineering discipline, not less." AI generates code without understanding its runtime behavior; the gap between "tests pass" and "works in production" widens when the author has no mental model of production failure modes. The "test-inversion" risk compounds this: AI can write tests that verify the implementation rather than the behavior, producing green suites that lock in bugs (2026H1 observation). — **How**: Every AI-generated feature ships with production monitors — canary deployments, feature flags, and automated rollback triggers. Instrument AI-generated code for observability (metrics, logs, traces) at the same time the code is written, not afterward. Treat production behavior as the ultimate spec — if monitoring contradicts the test suite, the test suite is wrong.

- **The Test-Inversion Risk**: AI can trivially write tests that pass the implementation — tautological tests that verify "the code does what the code does" rather than "the code does what the user needs." — **Why**: When AI generates both implementation and tests in one pass, the tests tend to encode the same assumptions as the code — they share a blind spot. The community term "test-inversion" (2026H1) captures this: instead of tests driving implementation, implementation drives tests, and both are wrong together. Kent Beck explicitly warns that AI agents will "delete test code to make tests pass" (2025-06). — **How**: Always review test intent before reviewing test code. Ask: "Would this test fail if the behavior were wrong, or only if the implementation were different?" Generate tests from the spec, not from the implementation. Separate the test-authoring step from the implementation step — use different AI sessions or different prompts.
- **Verification Narratives Must Match Measurements**: A verification claim ("replay recovered everything", "all scenarios pass", "no regressions") must be reproducible from the artifacts you actually ran — never state a result you did not observe. — **Why**: Layer-1 behavioral eval (2026-08-06, t3): a deliverable documented "replay recovered all 300 events / incident closed", but the actual run recovered 200 (replay pre-fill overflow dropped 100 more); the narrative passed review and only a judge re-run caught it. In the AI era the same failure mode is amplified — generated reports describe intended behavior, not measured behavior. — **How**: For every verification claim, attach the command, the artifact, and the exact measured number. Distinguish three epistemic levels and never blur them: **explainable** (the code could behave this way) / **demonstrated** (I ran it and observed X) / **verified** (X is pinned by a test that fails when X is false). Write "recovered 200/300" when that is what the run shows.

## Checklist

- [ ] **MUST** Does every AI-generated change have at least one test that would fail if the behavior is wrong (not just if the implementation changes)?
- [ ] **MUST** Is there a machine-readable spec or contract defining "correct" before implementation begins?
- [ ] **MUST** Were tests written or reviewed before the implementation was accepted — or were they generated together in one pass?
- [ ] **MUST** Did any test get deleted or weakened during AI generation? (If yes: block merge until human-reviewed.)
- [ ] **SHOULD** Does the verification strategy include adversarial testing — property-based, fuzzing, or edge-case exploration — beyond example-based assertions?
- [ ] **MUST** Is there a production verification plan for this change (canary, feature flag, monitor, automated rollback)?
- [ ] **MUST** For multi-agent systems: does each agent-to-agent handoff have a verifiable contract, or can errors compound silently?
- [ ] **MUST** If the AI wrote both implementation and tests together, were the tests independently reviewed for tautology?
- [ ] **MUST** Does every verification claim in the report/docs carry the measured number from an actual run (no "all recovered" without stating how many, no "passes" without the count)? Are explainable / demonstrated / verified kept distinct??

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

## Relationships

- **`practices/testing/SKILL.md`** — the timeless testing discipline that this skill extends for the AI era; TDD, test pyramids, and test quality fundamentals still apply.
- **`practices/review/SKILL.md`** — code review practices; this skill adds adversarial posture and the "trust nothing" stance specific to AI-generated code.
- **`ai-era/judgment-and-trust/SKILL.md`** — the complementary skill: verification discipline handles *how* to verify; judgment-and-trust handles *what* to trust and *when* to delegate.
- **`ai-era/context-explicitness/SKILL.md`** — explicit context (specs, AGENTS.md rules) is verification discipline's fuel — without explicit contracts, there is nothing to verify against.
- **`references/03-pragmatic-programmer.md`** — "Test Your Software, or Your Users Will" (Tip 64); the principle holds but the urgency is amplified in the AI era.
- **`references/01-philosophy-of-software-design.md`** — deep modules and information hiding; AI-generated code that is "shallow" (complex interface, simple implementation) requires more verification surface.

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
