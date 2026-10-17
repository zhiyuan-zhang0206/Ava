# 01 · A Philosophy of Software Design

**Author**: John Ousterhout (Stanford professor, inventor of Tcl, RAMCloud lead)
**Position**: Not a book about specific technologies — it answers one proposition: **the single core proposition of software design is reducing complexity.**
**Review status**: ✅ Approved by user (2026-08-05); English edition

---

## 0. One-Sentence Core

> The essence of software complexity = anything about a system's structure that makes it hard to understand and modify. Every design principle is a corollary of this proposition: reduce complexity.

**Extension**: The goal is not zero complexity (impossible) but **eliminating unnecessary complexity and concentrating the necessary kind where it can be managed**. Every design decision should ask: "Does this increase or decrease overall complexity?"

---

## 1. The Epistemology of Complexity

### 1.1 Definition and Mathematical Formulation

Complexity = anything related to a software system's structure that makes it hard to understand and modify. Practical criterion: **hard to add new features, or fixing bugs frequently introduces new problems**.

Mathematical formulation: **C = Σ(cₚ × tₚ)** — total complexity = Σ(complexity of each part × the fraction of developer time spent on that part).

The insight of this formula: **complexity is weighted**. A module that is very complex but almost never modified does limited harm; a moderately complex module that is modified daily (e.g., core orchestration, state routing) drags down the whole project. When assessing complexity, first ask "where do people touch every day?"

### 1.2 Three Symptoms (Recognition Signals)

| Symptom | Manifestation | How to recognize |
|---|---|---|
| **Change Amplification** | A simple change requires edits in many places | How many files does one requirement touch? More than 2–3 is an alarm |
| **Cognitive Load** | Too much information is required to complete a task | How much must a newcomer read before they can modify this module? |
| **Unknown Unknowns** | Don't know what to change, don't know what constraints exist | The worst symptom — change A breaks B and nobody could have known in advance |

**Example**: One requirement forces simultaneous changes to the DB schema, API docs, frontend form, and backend validation — four-way coupling is change amplification; understanding the framework's initialization internals just to add a simple feature is cognitive load; changing module A's config without knowing module B also reads it is unknown unknowns.

### 1.3 Two Causes

- **Dependencies**: invisible relationships between code. Dependencies cannot be eliminated (an API is itself a dependency); the goal is to **reduce the number of dependencies and make the remaining ones as simple and obvious as possible**.
- **Obscurity**: important information is not obvious — overly generic variable names, business logic buried in a giant config, magic numbers, hidden side effects.

Dependencies → change amplification + cognitive load; obscurity → unknown unknowns.

### 1.4 Key Property: Complexity Accumulates Incrementally

Complexity is not caused by a single catastrophe; it accumulates **from hundreds of tiny dependencies and obscurities, "death by a thousand cuts"**. Corollaries:

- No single "revolutionary rewrite" can fix a complexity problem — daily governance is the only cure
- **Every small decision matters** — one "just this once" tactical compromise is the first cut

---

## 2. Tactical vs Strategic Programming

### 2.1 Tactical Programming

Character: get features working quickly; system design is not a priority; kludges for a bad output, hardcoded special cases, large fragile regexes — all considered reasonable compromises.

**Tactical Tornado**: a programmer who ships a lot but leaves wreckage — celebrated short-term, destructive long-term. Each "just this once" special case makes control flow more fragile, until the system becomes something nobody dares to change.

### 2.2 Strategic Programming

Character: **working code is not enough** — the primary goal must be an excellent system design.

- Invest **10–20% of total development time** in design (active: finding the simplest interface for a module, whiteboard sequence diagrams; passive: fixing design problems immediately instead of patching)
- Short-term it looks 10–20% slower, but complexity growth is suppressed and medium/long-term speed improves markedly
- For any system: stop and think the architecture through before coding — module boundaries, state machines, observability — rather than letting the system grow into a black box

### 2.3 Trap: How Agile and TDD Slide into Tactical Programming

- Agile's iterative mindset easily focuses on piling up "features" while neglecting "abstraction" accumulation
- TDD forces attention on single-feature pass at tiny granularity, **lacking a feedback loop that drives developers to improve overall design**

---

## 3. Module Design: The Three-Piece Set

### 3.1 Deep Modules — the book's most important concept

Module = interface (everything a caller needs to know; the What) + implementation (the How).

**Module depth = functionality provided ÷ interface-imposed cost**. A deep module hides powerful functionality and large implementation complexity behind a simple interface.

**Classic cases**:
- ✅ Unix file I/O: `open/read/write/lseek/close` — five syscalls hiding disk sector management, permissions, interrupts, concurrency scheduling, memory caching — hundreds of thousands of lines of complexity
- ❌ Early Java I/O: reading one serialized object requires nesting `FileInputStream → BufferedInputStream → ObjectInputStream` — buffering should have been a default in the lower layer; the shallow design forces developers to care about irrelevant details

**Classitis**: the opposite of deep modules — creating many small shallow classes, each interface adding cognitive tax while hiding nothing. Merging related shallow classes into deep modules is the cure.

**Contrast**: a "deep" interface like `file.read(path)` — callers need not know about disk blocks, caching, encoding; a "shallow" interface requiring callers to configure buffer size, encoding, lock mode one by one pushes complexity onto the caller.

### 3.2 Information Hiding and Leakage

**Information hiding** (due to Parnas): each module encapsulates knowledge representing key design decisions; that knowledge should not appear in its interface.

**Information leakage**: the same design decision reflected in multiple modules simultaneously — the most dangerous design signal. Leak types:

- **Interface leakage**: the interface exposes implementation details directly
- **Back-door leakage**: two modules share the same format/convention but the interface doesn't show it — more subtle than interface leakage (e.g., two classes each parse the same JSON format)
- **Temporal leakage**: see "temporal decomposition" below
- **Decorator leakage**: a decorator is forced to replicate the whole interface to extend functionality

**Temporal decomposition — the most common leakage source**: module structure divided by the chronological order of operations rather than by knowledge encapsulation.

- ML example: `TrainDatasetCreator / TrainingModule / TestDatasetCreator` — the training and test set creators share data source structure, feature engineering, and outlier handling, yet are split by temporal order
- Generic example: `RequestReader → RequestParser → RequestHandler` — HTTP protocol format knowledge leaks across three stages; a protocol change forces edits in three places. Fix: encapsulate as one "HTTP protocol handling" knowledge domain

### 3.3 General-Purpose vs Special-Purpose Modules

Misconception: YAGNI says write only special-purpose code for current needs — but this often yields duplicated, fragile special-case logic.

**The sweet spot: "make modules somewhat general-purpose"** — the internal implementation reflects current needs, but the external interface does not reflect a specific business use case; it is general enough to support multiple uses.

**Case**: a text editor providing two special-purpose methods for "backspace/delete" = UI business logic leaking into the underlying storage; the fix is a general interface `insert(position, text)` / `delete(start, end)`, with the UI layer mapping backspace to "compute previous character position + call generic delete".

**Anti-pattern**: writing one special function for "generate quarterly report summary" and another for "generate weekly report summary" — special-case logic piles up; the fix is a generic `summarize(document, format)` parameterized by the caller.

---

## 4. Concrete Techniques for Reducing Complexity

### 4.1 Different Layer, Different Abstraction

A well-designed system gives each layer an abstraction **strikingly different** from the layers above and below. Similar abstractions in adjacent layers = danger signal, two typical forms:

- **Pass-through method**: a method that forwards to the next layer with the same signature and no logic — adds interface complexity without functionality. Cure: eliminate the middle layer, or merge into a real abstraction
- **Pass-through variable**: a variable forced through a chain of intermediate methods that don't need it. Cure: context object

**Warning**: overusing decorators/proxies to wrap a low-level library produces many pass-through methods — a high-level dispatcher whose parameters are identical to the underlying API has added no abstraction value.

### 4.2 Pull Complexity Downwards

When facing unavoidable complexity: **let the module's author bear the pain so callers stay comfortable** — a module has far more users than authors; a simple interface beats a simple implementation.

**Configuration-parameter abuse = pushing complexity upward**: exposing hundreds of config items (retry count, timeouts, buffer thresholds, truncation policy) and requiring callers to understand internals to set them — the anti-pattern. Fix: self-adapting internals (dynamically compute remaining budget, automatic sliding windows).

### 4.3 Define Errors Out of Existence

Error handling is one of the worst complexity sources. (Ousterhout argues the point from design experience; an independent empirical anchor: Yuan et al., OSDI 2014, found 92% of catastrophic failures in distributed data-intensive systems came from incorrect handling of non-fatal errors — not from the errors themselves.) The most effective tool is **redefining API semantics so special cases stop being exceptions**:

- ✅ Unix deleting an open file: succeeds immediately, marks deleted, frees on handle close — eliminates Windows' "file in use" exception class
- ✅ Python slices silently clamp out-of-range vs Java substring throwing — the former eliminates defensive-check burden

**Three companion techniques** (turning exceptions into states):

| Technique | Approach | Effect |
|---|---|---|
| **Exception masking** | Low layer catches and silently handles (internal exponential backoff on rate limits) | High-level business logic never notices network hiccups |
| **Exception aggregation** | Don't catch in every small method; let exceptions propagate to a single top-level handler | One global fallback policy |
| **State-machine self-healing** | Define "data not found" as a normal state rather than an exception (return empty collection instead of throwing) | Callers handle empty results on the normal path; control flow is not interrupted |

### 4.4 Design It Twice

For important designs, conceive at least **two substantially different alternatives** before choosing — reject the brain's first instinct (it is usually merely the most familiar, not the best). Example: for a tool-calling network, weigh "static DAG" vs "fully dynamic routing" on the whiteboard before committing.

### 4.5 Performance and the Critical Path

- **Measure before optimizing**: programmers' intuition about bottlenecks is highly unreliable; profile data decides
- **Design around the critical path**: strip all exception branches and special cases, think through the minimal code the happy path must execute; merge special cases into one or two checks at the path's start, branching out only on failure. RAMCloud case: refactor doubled core-path speed and cut code by 20%
- **Generalization**: design the most common request's path to be the shortest — special handling becomes one or two checks at the start, branching out only on failure

---

## 5. Comments and Expression

### 5.1 The Philosophy of Comments

"Good code needs no comments; code is self-documenting" — **Ousterhout explicitly refutes this as a myth**. Code can only precisely describe How; it can never capture the author's high-level abstractions (What), design trade-offs, and historical reasons (Why).

**Core rule**: comments should describe what is not obvious from the code — intent, trade-offs, assumptions, invariants.

- **Interface comments matter most** — they define what the abstraction promises
- **Implementation documentation contaminating the interface** = danger signal: public interface comments dwelling on low-level implementation details destroy information hiding
- High-level comments (abstract concepts, cross-module constraints) are worth more than line-by-line low-level comments
- **Comments must be updated with the code** — a stale comment is more misleading than no comment

### 5.2 Write the Comments First — the contrarian practice

Write interface comments before the class/method body. Threefold value:

1. Forces strategic thinking instead of jumping straight into tactical coding
2. **A design evaluation tool**: if you cannot write a short clear interface comment, or are forced to enumerate many special conditions → the design is too complex, the abstraction is flawed — start over
3. **Comments are the handoff**: clear interface comments and cross-module design-decision comments are the only thread by which newcomers (or anyone unfamiliar with the code) understand architectural boundaries — implicit conventions that are not written down are lost

### 5.3 Code Should Be Obvious; Consistency

- **Obvious**: readers can quickly guess macro behavior without reading every line. Design for readability, not writability — reject semantically vague generic containers like `Pair/Tuple`; use strongly typed data classes
- **Consistency**: knowing part lets you predict the rest — a new operation's schema, error handling, and calling conventions match existing ones. Derived from consistency: orthogonality (independent features don't affect each other), propriety (don't introduce things irrelevant to the purpose), generality (core mechanisms apply to multiple situations)

---

## 6. Modifying Code and System Evolution

### 6.1 Strategic Evolution

The biggest temptation when modifying existing code is the "minimal invasive change" — adding if-else special cases, hardcoding around abstraction boundaries; the architecture rots accordingly. Correct posture: **step back before the change and ask "given the new requirement, is the original design still optimal?"** If not, refactor as you implement, so the system looks as if it was designed with this feature from the start.

### 6.2 The Boy Scout Rule

Leave the codebase better than you found it. Invest 10–20% architecture improvement in every change to offset complexity entropy.

---

## 7. Critique of Popular Practices

### 7.1 TDD
TDD forces attention on single-feature pass at tiny granularity, **lacking a feedback loop for improving overall system design**. **The unit of incremental development must be abstraction, not features.**

### 7.2 Design Patterns
The biggest risk is "when you have a hammer, everything looks like a nail" — forcing simple problems into deeply nested patterns, adding cognitive burden. **Start simple, scale smart.**

---

## 8. Red Flags Quick Reference (Design Defect Checklist)

| Red flag | Symptom | Architectural harm |
|---|---|---|
| **Shallow module** | Interface complexity ≈ implementation complexity | Hides nothing; pure cognitive tax |
| **Information leakage** | Same design decision in multiple modules | Change amplification; one edit breaks many places |
| **Temporal decomposition** | Modules split by execution order | The culprit behind information leakage |
| **Overexposure** | API forces rarely-used parameters on callers | Cognitive load pushed onto callers |
| **Pass-through method** | Forwarding with no logic to the next layer | Meaningless layer; shallow module |
| **Special-general mixture** | Business hardcoding inside low-level infrastructure | Infrastructure fragile and unreusable |
| **Conjoined methods** | Two methods with heavy implicit state dependency; must read both together | Unknown unknowns |
| **Implementation docs polluting interface** | Interface comments dwelling on implementation details | Breaks information hiding; comments go stale |
| **Nonobvious code** | Can't guess behavior without reading the implementation | Violates the obviousness principle |

---

## 9. Relationship to the Other Books / Skill Architecture

- This book is the skeleton source of the **complexity-management** sub-skill (definition, formula, symptoms, causes, pull-complexity-down, define-errors-out-of-existence)
- **Deep modules / information hiding** → module design; **naming / obviousness** echo Evans' Ubiquitous Language
- **Tactical vs strategic programming** echoes Brooks' "design rationales" and Thomas & Hunt's "tracer bullets and prototypes"
- Heuristics ①④⑥⑦⑧⑨⑩ are all influenced by this book (used as quick-reference tags)
