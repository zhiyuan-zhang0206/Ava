# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Brooks, *The Design of Design* (2010) — §2 (conceptual integrity, one-owner principle, rejection of committee design)
- Ousterhout, *A Philosophy of Software Design* (2018) — §8 (red-flag checklist: nine design-defect signals)
- Thomas & Hunt, *The Pragmatic Programmer* (2019) — Tips 37–39 (design by contract as review criterion), Tips 72–73 (security review)
- addyosmani/agent-skills, `code-review-and-quality` — five-axis review (architecture, semantics, security, test coverage, documentation)
