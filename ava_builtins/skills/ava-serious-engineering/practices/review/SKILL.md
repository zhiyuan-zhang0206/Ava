---
name: review
description: Use when reviewing a PR, a design document, or a code change — to judge whether it preserves conceptual integrity, catches red-flag design defects, and meets the five-axis quality standard (architecture, semantics, security, test coverage, documentation).
---

# Review

## One-Sentence Core
> Code review is a conceptual-integrity gate — every change must be checked against the system's Design Concept, not just its syntax; a review without a concept is rubber-stamping.

## Core Principles

- **Review against conceptual integrity first**:Before checking style or logic, ask: does this change fit the system's unifying concept, or does it pull in a different direction? — **Why**:Brooks (§2): conceptual integrity is the most important property of a design; without it, the system becomes a committee product — bloated, inconsistent, nobody dares say no. Every change either reinforces the concept or erodes it. — **How**:State the Design Concept explicitly (one paragraph in the project's AGENTS.md or architecture doc). For every PR, ask: "Does this change make the concept clearer or muddier? Does it use the same concepts with the same shapes as the rest of the system?"

- **Review across five axes, not just syntax**:Architecture, semantics, security, test coverage, and documentation — a PR that passes linting can still fail on any of these. — **Why**:Addy Osmani's code-review-and-quality skill (addyosmani/agent-skills): narrow review that only checks style and logic misses systemic defects — architecture violations, semantic drift, missing tests, undocumented assumptions. — **How**:Run a five-axis pass on every non-trivial PR:

  1. **Architecture**: are modules deep (Ousterhout)? Is coupling minimal? Does the change respect bounded-context boundaries (Evans/Vernon)?
  2. **Semantics**: do names match the ubiquitous language (Evans)? Are contracts explicit (Thomas & Hunt, Tip 37)? Is behavior obvious from the interface (Ousterhout)?
  3. **Security**: is untrusted input isolated? Are attack surfaces minimal? (Thomas & Hunt, Tips 72–73)
  4. **Test coverage**: do tests cover the changed behavior and its edge cases? (see `practices/testing`)
  5. **Documentation**: is the *why* documented, not just the *what*? Are architectural decisions recorded?

- **Run the red-flag checklist on every review**:Ousterhout's nine red flags are a pre-flight diagnostic — catch design defects before they ship. — **Why**:Ousterhout (§8): each red flag is a proven signal of a design defect — shallow module, information leakage, temporal decomposition, overexposure, pass-through method, special-general mixture, conjoined methods, implementation docs polluting interface, nonobvious code. A reviewer who doesn't check for these is missing the most common sources of future rot. — **How**:Keep the nine-flag table (references/01 §8) visible during review. For every changed module, run the list: "Is this a shallow module? Is information leaking? Is the interface obvious?" Flag any hit.

- **Small batches, fast rhythm**:A PR over 400 lines gets a shallow review (400 is a heuristic threshold, not a law — the real question is whether the reviewer can hold the whole change in mind); a PR that sits for three days accumulates context-switch cost. — **Why**:Brooks' law extended: cognitive load is the bottleneck of review, not calendar time. A reviewer can hold roughly 400 lines in working memory; beyond that, review becomes sampling, not understanding. — **How**:Target PRs under 400 lines. Review within 24 hours. If a change is larger, split it into a stack of small, reviewable PRs, each with its own rationale.

- **Review is not negotiation — one owner decides**:Every part of the system has exactly one owner at any moment; review is synchronization and error detection, not collective design. — **Why**:Brooks (§2): "Don't believe in fantasy collaboration" — equal negotiation yields committee design. The reviewer catches defects; the author owns the design decision. — **How**:The reviewer flags problems with evidence (red-flag name, principle violated, concrete harm). The author addresses or rebuts. When they disagree, the architect (or designated owner) decides — the review thread is not a design-by-committee forum.

## Checklist
- [ ] **MUST** Does this change preserve or improve conceptual integrity?
- [ ] **MUST** Architecture axis: are modules deep? Is coupling minimal? Are context boundaries respected?
- [ ] **MUST** Semantics axis: do names match the ubiquitous language? Are contracts explicit?
- [ ] **MUST** Security axis: is untrusted input isolated? Are new attack surfaces justified?
- [ ] **MUST** Test coverage axis: do tests exist for the changed behavior and its edge cases?
- [ ] **SHOULD** Documentation axis: is the *why* recorded — design decisions, trade-offs, constraints?
- [ ] **SHOULD** Red-flag scan: any shallow modules, information leakage, pass-through methods, nonobvious code?
- [ ] **SHOULD** Is the PR under ~400 lines (heuristic threshold)? If not, can it be split?

## Anti-Patterns
- **Rubber-stamp review**:"LGTM" without evidence of a five-axis pass. → Every review must cite at least one axis explicitly (e.g., "Architecture: modules are deep, no information leakage. Semantics: names match the domain glossary.")
- **Architecture-free review**:Checking syntax and logic while ignoring whether the change fits the system's design. → Always start from the Design Concept; if the project has no written Design Concept, flag that as the first issue.
- **Mega-PR review**:A 2000-line PR gets skimmed; defects slip through. → Split into small PRs; if unsplittable, review by commit, not by diff.
- **Reviewing without running**:Reviewing from the diff alone misses behavior the code implies but doesn't show. → Check out the branch and run the tests; exercise the changed path manually if the test coverage is thin.
- **Style over substance**:Nitpicking formatting while missing a shallow-module architecture defect. → Automate style (linter in CI); human review time goes to architecture, semantics, and design.

## Examples

### 1. Architecture-blind review
```
❌ Bad: "LGTM, code is clean."
    (PR adds a pass-through layer — every method forwards to the next layer
     with no logic. The reviewer didn't notice.)

✅ Good: "Architecture: the `PriceCalculator` interface is a pass-through
    method — it forwards every call to `InternalPricer` without adding
    behavior. Red flag: shallow module (Ousterhout §8). The abstraction
    hides nothing and adds a layer callers must learn. Can we remove it
    and let callers use `InternalPricer` directly, or give this module
    a real responsibility?"
```

### 2. Semantic drift
```
❌ Bad: PR adds a `User.deactivate()` method. Reviewer checks logic,
    approves. But the domain glossary says "suspend" for temporary
    and "terminate" for permanent — "deactivate" introduces a third
    term nobody can define.

✅ Good: "Semantics: the domain glossary defines 'suspend' (temporary)
    and 'terminate' (permanent). What does 'deactivate' mean?
    If it maps to one of these, use the existing term. If it is a new
    concept, update the glossary and explain the distinction."
```

### 3. Red-flag catch
```
❌ Bad: Reviewer approves a PR where `OrderProcessor` reads
    `config.global_tax_rate` directly. The implementation is correct,
    the test passes, the review ends.

✅ Good: "Architecture: `OrderProcessor` reads a global config value
    (`global_tax_rate`). Red flag: special-general mixture — business
    logic (tax calculation) is coupled to infrastructure (config reading).
    `OrderProcessor` should receive tax rate as a parameter; the config
    binding belongs at the composition root."
```

## Relationships
- `principles/conceptual-integrity` — the review's north star: every change must be consistent with the system's unifying concept
- `principles/complexity-management` — the red-flag checklist is a complexity early-warning system
- `principles/ubiquitous-language` — semantic review checks that code names match the domain language
- `practices/testing` — review includes test-coverage assessment; see testing skill for coverage standards
- `practices/maintenance` — review catches broken-window candidates before they become rot
- `references/01-philosophy-of-software-design.md §8` — red-flag quick reference (nine design-defect signals)
- `references/02-design-of-design.md §2` — conceptual integrity as the overriding review criterion
- addyosmani/agent-skills, `code-review-and-quality` — five-axis review framework (ecological reference)

## Sources
- Brooks, *The Design of Design* (2010) — §2 (conceptual integrity, one-owner principle, rejection of committee design)
- Ousterhout, *A Philosophy of Software Design* (2018) — §8 (red-flag checklist: nine design-defect signals)
- Thomas & Hunt, *The Pragmatic Programmer* (2019) — Tips 37–39 (design by contract as review criterion), Tips 72–73 (security review)
- addyosmani/agent-skills, `code-review-and-quality` — five-axis review (architecture, semantics, security, test coverage, documentation)
