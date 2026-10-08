---
name: review
description: "Reviews software changes and designs. Use when assessing correctness, architecture, security, test evidence, or maintainability in a requested review."
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

## Relationships
- `principles/conceptual-integrity` — the review's north star: every change must be consistent with the system's unifying concept
- `principles/complexity-management` — the red-flag checklist is a complexity early-warning system
- `principles/ubiquitous-language` — semantic review checks that code names match the domain language
- `practices/testing` — review includes test-coverage assessment; see testing skill for coverage standards
- `practices/maintenance` — review catches broken-window candidates before they become rot
- `references/01-philosophy-of-software-design.md §8` — red-flag quick reference (nine design-defect signals)
- `references/02-design-of-design.md §2` — conceptual integrity as the overriding review criterion
- addyosmani/agent-skills, `code-review-and-quality` — five-axis review framework (ecological reference)

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
