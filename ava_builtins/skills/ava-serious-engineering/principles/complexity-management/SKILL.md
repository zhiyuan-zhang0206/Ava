---
name: complexity-management
description: "Diagnoses and reduces change amplification, cognitive load, and unknown unknowns in software. Use when a system is hard to understand or modify, one change touches many files, or bugs repeatedly surprise the team."
---

# Complexity Management

## One-Sentence Core
> The single core proposition of software design is reducing complexity — anything about a system's structure that makes it hard to understand and modify. Every other design principle is a corollary of this one.

## Core Principles

- **Complexity is measurable by its symptoms, not its size**:Complexity = Change Amplification + Cognitive Load + Unknown Unknowns. A small module touched daily can be more harmful than a large module nobody touches. — **Why**:Ousterhout's formula C = Σ(cₚ × tₚ) weights each part by the fraction of developer time spent on it. A module that blocks every feature is the real bottleneck, not the one with the most lines. — **How**:When a requirement arrives, count how many files it touches (change amplification), how much context a newcomer needs to read before modifying the module (cognitive load), and whether anyone can predict what will break (unknown unknowns). More than 2–3 files for one requirement is an alarm.

- **Complexity accumulates incrementally — "death by a thousand cuts"**:No single catastrophe creates an unmaintainable system; hundreds of tiny dependencies and obscure decisions do. — **Why**:Every "just this once" tactical compromise — a hardcoded special case, a duplicated constant, a method that reaches across layers — is one cut. Individually they are survivable; collectively they lock the system. — **How**:Treat every code review as a complexity checkpoint. Ask of every new line: "Does this increase or decrease overall complexity?" Fix complexity regressions on sight (the Boy Scout rule: leave the codebase better than you found it). Never let a "temporary" workaround survive one sprint without a plan.

- **Deep modules hide complexity; shallow modules create it**:A module's value is the ratio of functionality it provides to the interface cost it imposes on callers. Deep modules hide powerful implementation behind a simple interface; shallow modules add cognitive tax without hiding enough. — **Why**:Unix `open/read/write/close` hides disk scheduling, caching, permissions, and concurrency behind five calls. Early Java I/O forced callers to nest `FileInputStream → BufferedInputStream → ObjectInputStream` — buffering should have been a default, not a caller's responsibility. — **How**:For each module, compare its interface surface area (number of methods, parameters, exposed concepts) to the implementation complexity it hides. If the interface is nearly as complex as the implementation, the module is shallow — merge it with related shallow modules or redesign the abstraction. Write interface comments first: if you cannot describe the module's contract in a short clear paragraph, the interface is too complex.

- **Information hiding is the primary weapon against dependencies**:Each module encapsulates knowledge representing one design decision; that knowledge must not leak into other modules through interfaces, shared formats, or temporal coupling. — **Why**:When one design decision (e.g., storage format, protocol version, business rule) is reflected in multiple modules simultaneously, any change to that decision forces coordinated edits across all of them — the very definition of change amplification. — **How**:Audit for four leakage patterns: (1) interface leakage — implementation details in the public API; (2) back-door leakage — two modules independently parse the same format; (3) temporal decomposition — modules split by execution order instead of knowledge domain; (4) decorator leakage — a wrapper forced to replicate the entire interface. The most common culprit is temporal decomposition (e.g., `RequestReader → RequestParser → RequestHandler` — protocol knowledge leaks across three stages).

- **Start simple, then evolve — a complex system that works grew from a simple system that worked**:Gall's Law states that a complex system designed from scratch never works and cannot be patched into working; you must start over with a working simple system. — **Why**:Complexity that emerges through evolution carries implicit knowledge — trade-offs, edge cases, and interactions that were discovered, not designed. A system designed complex from day one embeds assumptions that have never been tested against reality. — **How**:Ship the simplest version that solves the core problem first, then add complexity only in response to real (not imagined) needs. When the system grows complex, ask whether the added complexity genuinely came from real requirements or from premature architecture. Ashby's Law of Requisite Variety (as popularized by Weinberg in general-systems thinking) adds: the control system must be at least as complex as the system it governs — but no more. Match complexity to the problem, not to your ambition.

- **Pull complexity downwards**:When complexity is unavoidable, let the module author bear the pain so callers stay comfortable. A module has far more users than authors; a simple interface beats a simple implementation. — **Why**:Every complexity exposed in an interface is multiplied across every caller. A configuration parameter that requires the caller to understand internal retry logic, buffer thresholds, or truncation policy pushes complexity upward — and every caller pays the tax. — **How**:Replace caller-facing configuration with self-adapting internals (dynamic budget computation, automatic sliding windows). Default to reasonable behavior; expose knobs only when callers genuinely need different behavior, not "just in case."

- **Strategic programming invests in design; tactical programming borrows against it**:Working code is not enough — the primary goal must be an excellent system design. Invest 10–20% of total development time in active and passive design improvement. — **Why**:Tactical programming (get features working quickly, skip design) looks faster in the short term but accumulates complexity that slows every subsequent feature. The "Tactical Tornado" — a programmer who ships a lot but leaves wreckage — is celebrated short-term and destructive long-term. — **How**:Before coding any non-trivial feature, stop and think through module boundaries, state machines, and data flow. When modifying existing code, ask "given the new requirement, is the original design still optimal?" If not, refactor as you go — make the system look as if it was designed with this feature from the start.

## Checklist

- [ ] **MUST** Can a simple requirement change be completed by editing 2–3 files or fewer? If more, which dependency is causing the amplification?
- [ ] **MUST** Can a newcomer understand how to modify a core module after reading its interface comments (not its implementation)?
- [ ] **SHOULD** For the module touched most frequently: is its interface surface area (methods × parameters) substantially smaller than its implementation complexity?
- [ ] **MUST** Does each module hide exactly one design decision? If two modules both "know" the same thing (format, rule, protocol version), where is the leakage?
- [ ] **SHOULD** Are adjacent layers in the system providing strikingly different abstractions, or are they passing through the same concepts?
- [ ] **SHOULD** Could the system have started simpler? If complexity was added before real need, can it be removed?
- [ ] **MUST** Is complexity pushed down to the implementation layer rather than exposed to callers as configuration or required knowledge?
- [ ] **SHOULD** In the last sprint, was at least 10% of time spent on design improvement (refactoring, simplifying interfaces, fixing information leakage)?

## Anti-Patterns

- **Tactical Tornado**:Shipping features fast while leaving design wreckage — celebrated short-term, destructive long-term. → alternative: Invest 10–20% of each feature's time in design; refactor as you go instead of patching.
- **Death by a thousand cuts**:Accepting "just this once" workarounds — hardcoded special cases, duplicated constants, methods reaching across layers — because each alone seems harmless. → alternative: Treat every review as a complexity checkpoint; fix regressions on sight.
- **Shallow module proliferation**:Creating many small classes each with a large interface-to-implementation ratio (Classitis). → alternative: Merge related shallow classes into deep modules; a module should hide more than it exposes.
- **Temporal decomposition**:Splitting modules by execution order (`Reader → Parser → Handler`) rather than by knowledge domain. → alternative: Group by the design decision each module encapsulates; one knowledge domain = one module.
- **Premature complexity**:Designing for imagined future requirements before the simple version has proven itself. → alternative: Follow Gall's Law — ship the simplest working system first; add complexity only when real requirements demand it.

## Examples

**Example 1: Deep vs Shallow Module**

❌ Bad (shallow):
```python
class FileReader:
    def __init__(self, path, buffer_size=4096, encoding='utf-8',
                 lock_mode='shared', cache_policy='lru'):
        ...
    def read_bytes(self, count): ...
    def set_buffer_size(self, size): ...
    def get_encoding(self): ...

# Caller must understand buffer_size, encoding, lock_mode, cache_policy
# Interface surface ≈ implementation complexity → shallow
```

✅ Good (deep):
```python
class FileReader:
    def __init__(self, path): ...
    def read(self) -> str: ...

# Caller only needs path and read()
# Implementation handles buffering, encoding, locking, caching internally
# Interface surface ≪ implementation complexity → deep
```

**Example 2: Information Leakage via Temporal Decomposition**

❌ Bad (temporal decomposition — same knowledge leaked across stages):
```python
class RequestReader:
    def parse_headers(self, raw: bytes) -> dict: ...  # knows HTTP format
class RequestParser:
    def validate_method(self, headers: dict) -> str: ...  # knows HTTP format
class RequestHandler:
    def extract_body(self, raw: bytes, headers: dict) -> bytes: ...  # knows HTTP format
# Changing HTTP version handling requires edits in all three classes
```

✅ Good (knowledge-domain decomposition):
```python
class HttpProtocol:
    def parse(self, raw: bytes) -> ParsedRequest: ...
    def serialize(self, request: ParsedRequest) -> bytes: ...
# One module owns all HTTP format knowledge; change it in one place
```

**Example 3: Gall's Law — Start Simple**

❌ Bad (premature complexity):
Building a microservice mesh with event sourcing, CQRS, and sagas for a startup's first user-facing feature — before a single customer exists.

✅ Good (evolve from simple):
Ship a single-process monolith with a simple database. When traffic grows and real bottlenecks emerge, extract services at the seams that actual usage reveals.

## Relationships

- `principles/conceptual-integrity` — Conceptual integrity is the organizational-level defense against complexity: one coherent Design Concept prevents the fragmentation that breeds dependencies and obscurity.
- `principles/error-handling` — Define Errors Out of Existence (§4.3 of Ousterhout) is a direct application of pulling complexity down: eliminate exception classes by redesigning semantics so error states become normal.
- `principles/dependency-management` — Dependencies are one of the two root causes of complexity; dependency management is the operationalization of information hiding at the module boundary.
- `principles/ubiquitous-language` — Obscurity (the second root cause) is often caused by naming that drifts from business meaning; ubiquitous language is the primary defense.
- `practices/design` — Strategic programming (invest 10–20% in design) is the daily practice that prevents complexity accumulation.
- `references/01-philosophy-of-software-design.md` §1–§4 — Full treatment of complexity definition, deep modules, and tactical-vs-strategic programming.

## Sources

- Ousterhout, *A Philosophy of Software Design* — complexity definition (C = Σ(cₚ × tₚ)), three symptoms, two causes, deep modules, information hiding, tactical vs strategic programming, pull complexity down. Primary source for this skill.
- Gall, *Systemantics* — Gall's Law: "A complex system that works is invariably found to have evolved from a simple system that worked."
- Ashby, *An Introduction to Cybernetics* (1956) — Law of Requisite Variety (the original source); Weinberg, *An Introduction to General Systems Thinking* — popularized the law in software-adjacent systems thinking.
- Brooks, *The Design of Design* — conceptual integrity as organizational defense against complexity (see `conceptual-integrity`).
