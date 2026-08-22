# 06 · The Sciences of the Artificial

**Author**: Herbert A. Simon (Nobel Prize in Economics 1978, Turing Award 1975, CMU professor — one of the few polymaths to shape AI, cognitive psychology, economics, and design theory simultaneously)
**Position**: Not a book about software — about a deeper question: **what is "design," and can it be a science?** The answer reshapes how we think about every artifact we build: software, organizations, interfaces, architectures. Three editions (1969, 1981, 1996) progressively deepened the argument.
**Review status**: ⏳ Detail level per user-approved 01 benchmark; content pending user review (2026-08-06)

---

## 0. One-Sentence Core

> The natural sciences are about *what is*; the sciences of the artificial are about *what ought to be*. Every software system is an artifact — a designed, contingent thing that could have been otherwise — and the principles that govern good design are different from the principles that govern discovery.

---

## 1. The Science of Design: Why "Design" Deserves a Discipline

### 1.1 The Natural vs. the Artificial

Simon draws a sharp line: **natural science** studies objects and phenomena that exist independent of human intention (atoms, galaxies, evolution). **The sciences of the artificial** study things that humans create to serve a purpose — buildings, organizations, computer programs, economic policies.

The key difference: an artifact can be judged by **how well it fulfills its purpose**. A bridge that collapses is objectively bad. A programming language that makes programs harder to read (given a fixed goal) is objectively worse than one that makes them clearer. This gives design a normative dimension that natural science lacks — there is no "bad" electron, but there are "bad" designs.

### 1.2 Design as Problem-Solving: The Generality

Simon's core claim: **all design is problem-solving under constraints.** Whether you are designing a compiler, an API, an organization chart, or a city plan, you are searching a large space of possibilities for a solution that satisfies a set of requirements. The space is too large to search exhaustively (bounded rationality — see §5), so designers use heuristics, decomposition, and satisficing rather than optimizing.

This unifies software design with every other design discipline. A software architect facing a combinatorial explosion of possible module decompositions is in the same fundamental situation as a mechanical engineer choosing among possible gear-train configurations.

### 1.3 The Curriculum Problem

Simon notes that engineering schools drifted from teaching *design* toward teaching *analysis* (physics, materials science, mathematics) in the 20th century — because analysis is easier to formalize and grade. The same drift happened in computer science: we teach algorithms, complexity theory, and type systems extensively, but spend far less time teaching *how to structure a system so that it can be understood and modified by humans over decades.* Simon's book is, in part, an argument that design can and must be taught as a rigorous discipline.

---

## 2. Near-Decomposability: The Architecture of Complex Systems

### 2.1 The Concept

This is Simon's most directly applicable contribution to software architecture. **A nearly decomposable system is one in which intra-component interactions are much stronger and more frequent than inter-component interactions.** The key word is *nearly* — perfect decomposability (zero cross-component interaction) is rare; what matters is the *ratio* of internal to external coupling.

Simon's insight: **near-decomposability is not just a human convenience — it is a property that allows complex systems to evolve and be understood.** If every component interacted strongly with every other component, evolution would be impossible (any change would require simultaneous coordinated changes across the whole system), and human understanding would be impossible (the cognitive load of understanding any part would require understanding every other part).

### 2.2 The Watchmaker Parable

Simon illustrates with a famous parable: two watchmakers, Tempus and Hora, each build watches of 1000 parts. Tempus builds his watches as single monolithic assemblies — if he is interrupted (a phone call), the partially assembled watch falls apart and he must start over. Hora builds his watches from stable subassemblies of 10 parts each, which are then combined into larger subassemblies of 10 subassemblies, and so on. If interrupted, Hora only loses the current subassembly.

The math: if the probability of interruption per part is p = 0.01, Tempus takes on average ~4000 times longer than Hora to complete one watch. **Hierarchical organization with stable intermediate forms is not just "nice to have" — it is an exponential advantage in any environment with uncertainty.**

The software translation: a module that can be built, tested, understood, and trusted independently is a "stable subassembly." A system built from such modules can survive interruptions (team changes, requirement changes, technology changes) exponentially better than a monolith where every part depends on every other.

### 2.3 Hierarchical Systems Are Inevitable

Simon argues that hierarchy is a near-universal property of complex systems — from cells → tissues → organs → organisms, to transistors → gates → processors → computers → networks. The reason is that hierarchy emerges naturally from the evolutionary dynamic: stable intermediate forms are selected for because they enable faster adaptation.

In software: layers, modules, packages, services — hierarchy is not an aesthetic choice. It is the only known structure that allows complex systems to be built and maintained by bounded beings (humans).

### 2.4 Testing and Near-Decomposability

A direct corollary: **a system is only as testable as its decomposability.** If module A cannot be meaningfully tested without spinning up modules B, C, and D, the system lacks near-decomposability. The ratio of integration tests to unit tests is a rough proxy — a high ratio signals that components are not genuinely independent.

---

## 3. The Inner Environment / Outer Environment Interface

### 3.1 The Model

Simon models every artifact as an **interface** between an **inner environment** (the artifact's substance and organization — what it *is*) and an **outer environment** (the surroundings in which it operates — what it *must cope with*). An artifact fulfills its purpose when the inner environment is *adapted* to the outer environment.

The central design task is then: **given a description of the outer environment and a goal, find an inner environment that achieves the goal.**

### 3.2 Why Interfaces Matter More Than Internals

A profound consequence of this model: **you can often evaluate whether a design is adequate by examining only the interface, without knowing the internals.** If the interface correctly matches the outer environment's demands, the internals can be substituted arbitrarily — this is the theoretical justification for abstraction, encapsulation, and API contracts.

Simon's formulation is more general than Parnas' information hiding (see 01 §3.2): it says that the interface does not merely *hide* complexity — it *represents the entire adaptation to the outer environment.* A well-designed interface is a complete specification of "what the system must cope with."

### 3.3 The Design Process: Simulating the Outer Environment

Since the outer environment is often complex, designers simplify it into a model. A traffic engineer models cars as fluid flows. A database designer models queries as access patterns. **The quality of a design depends critically on the fidelity of the outer-environment model — not just on the elegance of the inner environment.**

For software: the most catastrophic design failures come from misunderstanding the outer environment (what users actually need, what the deployment environment actually permits, what failure modes actually occur), not from internal implementation flaws. The inner environment can always be rewritten; a mismatch with the outer environment is existential.

---

## 4. The Architecture of Complexity: Hierarchy and Span

### 4.1 Span of Control

Simon observes that in nearly decomposable hierarchical systems, the span of control (number of immediate subordinates) tends to be narrow and roughly constant across levels. Biological systems: cells contain a limited number of organelles; organs contain a limited number of cell types. Organizations: a manager typically supervises 5–15 direct reports.

In software: a module should depend on a limited number of other modules; a class should have a limited number of direct collaborators. This is not an aesthetic preference — it is the empirically observed structure that enables evolution, maintenance, and understanding. When a module imports 40 other modules, it violates the span-of-control principle and becomes a cognitive and evolutionary bottleneck.

### 4.2 The Evolutionary Argument

Simon argues that complex systems evolve through a process of *assembling stable subassemblies.* Evolution does not design top-down; it builds from what already works. A new species does not evolve from scratch — it modifies an existing body plan. A new software feature should, analogously, build on existing stable abstractions rather than introducing new coupling across the system.

The implication: **evolutionary architecture is not a methodology choice — it is the only architecture that complex systems can have**, given that their designers are boundedly rational and their environments change over time.

### 4.3 Redundancy and Degradability

Nearly decomposable systems have an often-overlooked property: they degrade gracefully. If one subassembly fails, the damage is largely contained. A cell dies; the organ compensates. A service goes down; the circuit breaker isolates it. This property emerges from the architecture, not from any single component's design.

---

## 5. Bounded Rationality and Satisficing

### 5.1 The Cognitive Limit

Simon's Nobel-winning insight (developed in *Administrative Behavior* and woven throughout *The Sciences of the Artificial*): humans cannot optimize — the search space of real problems is too large, our knowledge is incomplete, and our computational capacity is finite. Instead, we *satisfice*: we search until we find a solution that is "good enough" relative to our aspiration level, then stop.

**This is not a flaw — it is a fact about the nature of cognition.** Any design methodology that assumes designers can fully enumerate alternatives and pick the optimal one is modeling designers as they are not.

### 5.2 Design Under Bounded Rationality

Practical consequences for software design:

- **Decomposition is cognitive scaffolding.** Breaking a problem into near-independent sub-problems is not just good engineering — it is the only way bounded beings can cope with complexity at all. A design that forces a new team member to understand the whole system before contributing anything is a design that ignores bounded rationality.
- **Aspiration levels adapt.** Designers adjust "good enough" based on what they find. If good solutions are easy to find, standards rise; if the search is hard, standards drop. This adaptation is automatic and often unconscious — a team that has been fighting a badly designed system for months has lowered its aspiration level without noticing.
- **Heuristics are not second-class.** Given bounded rationality, heuristics ("deep modules," "single responsibility," "don't repeat yourself") are not approximations to ideal rationality — they are the *only* kind of rationality available. A heuristic that reliably produces good-enough designs is more valuable than an "optimal" procedure that requires infinite computation.

### 5.3 The Link to Complexity

Bounded rationality and near-decomposability are linked: **hierarchical decomposition is the strategy by which boundedly rational agents build complex systems.** If we had infinite cognitive capacity, we could hold the entire design in our heads and near-decomposability would be optional. Because we don't, it is mandatory.

---

## 6. Design Representation and the Role of Abstraction

### 6.1 Thinking = Manipulating Representations

Simon's cognitive model: problem-solving is *search through a problem space*, and the structure of that space depends on the *representation* chosen. Change the representation, and a previously intractable problem becomes solvable. Example: solving a geometry problem with algebra (coordinate representation) vs. pure geometry.

For software: the choice of representation — the module decomposition, the data model, the type hierarchy, the state-machine encoding — is itself the most consequential design decision. A bad representation makes every downstream decision harder; a good one makes many problems trivial.

### 6.2 Abstraction as Dimensionality Reduction

An abstraction reduces the number of dimensions a designer must consider simultaneously. A function signature hides the implementation's variable space behind a 2–5 parameter interface. A module hides internal state behind a small set of operations. **Every abstraction is a bet about which dimensions can be safely ignored** — and bad abstractions ignore dimensions that turn out to matter.

Simon would say: the art of design is choosing representations that make the search space tractable while preserving the dimensions that determine success.

---

## 7. Key Takeaways for Software Design

1. **Near-decomposability is not optional** — it is the structural prerequisite for any complex system that must be built, understood, maintained, and evolved by bounded beings.
2. **The interface is the design** — the inner/outer environment model says that a well-specified interface *is* the adaptation to the environment. Get it right, and internals are substitutable.
3. **Hierarchy has mathematical justification** — the watchmaker parable is not a metaphor; it is a mathematical argument that stable intermediate forms confer exponential advantage.
4. **Satisficing is rational** — given bounded rationality, stopping at "good enough" is the optimal strategy. Methodologies that demand exhaustive optimization model humans incorrectly.
5. **Representation determines solvability** — before asking "how do we solve this," ask "are we representing it in a way that makes it solvable?"

---

## Source

Simon, Herbert A. *The Sciences of the Artificial.* 3rd ed. MIT Press, 1996. (Original: 1969; 2nd ed. 1981.)

Key chapters for software designers: Chapter 3 (The Science of Design), Chapter 7 (The Architecture of Complexity), Chapter 8 (The Psychology of Thinking).
