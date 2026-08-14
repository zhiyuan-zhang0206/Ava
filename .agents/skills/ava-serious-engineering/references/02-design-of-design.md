# 02 · The Design of Design

**Author**: Frederick P. Brooks (author of *The Mythical Man-Month*, father of IBM System/360, Turing Award laureate)
**Position**: A reflection written at age 70 — not about project management, but about a deeper question: **how design actually happens, and why we keep doing it badly**. "Design" here is broad: software, computer architecture, buildings, organizations.
**Review status**: ⏳ Detail level per user-approved 01 benchmark; content pending user review (2026-08-05)

---

## 0. One-Sentence Core

> The mark of a great design is **conceptual integrity** — unity, economy, clarity. And conceptual integrity comes from one authorized chief designer controlling the whole, never from committee negotiation.

---

## 1. The Process Model of Design: Why Waterfall Is Wrong

### 1.1 The Design Concept

Every artifact has three layers: Idea (conceptualization) → Implementation → Interaction. **The Design Concept is the team's "Platonic ideal system"** — more "real" than any specific implementation, the unifying vision behind all discussion. Its clarity and the team's consensus on it directly determine how elegant the product is.

### 1.2 The Rational Model and Its Fallacies

Engineers naturally model design as: define Goal → list Desiderata → define Utility Function → list Constraints → identify scarce resources → search the decision tree. **Brooks argues this model is wrong for real design problems**:

1. **The Goal is unclear at the start** — customers only react once they see something
2. **Desiderata change along the way** — after extensive house-design work, a small "where do guests' coats go" need moved the master bedroom from one end of the house to the other
3. **A utility function is nearly impossible to define** — trade-off work surfaces cheap high-value options that were never in the original desiderata
4. **Constraints keep changing** — new building codes, a discontinued chip, an API change
5. **Real designers don't work this way** — experts oscillate between sub-problems and sub-solutions, using partial solutions to understand the problem and new understanding to fix the solutions

> Schön: "The designer shapes the situation; the situation talks back; the designer responds by reflecting-in-action." Design is a conversation with the situation, not a one-way search.

### 1.3 Why Waterfall Won't Die: Sin and Contracts

In a perfect world (clients not greedy, architects selfless, implementers honest), cost-plus + Spiral would be optimal. **But people are "fallen": mutual distrust forces contracts; contracts force requirements to be fixed before design starts — and §1.2 showed that is impossible.** Waterfall persists not because it is good, but because the contract form demands it.

### 1.4 A Better Process: Spiral + Design-Build Integration

Recommends Boehm's Spiral Model (spiral upward: set goals → identify risks → prototype → evaluate), with explicit **contracting points** marked on the spiral. The traditional architectural model is better: client gives a **program** (not a specification) → conceptual design → iteration → detailed design → construction documents; the contract pays for service time, not a fixed deliverable.

---

## 2. Collaboration and Conceptual Integrity (a central chapter)

1. **Conceptual integrity above all**: St. Paul's Cathedral is great because Wren controlled the whole; Seymour Cray personally carried CDC 6600 from architecture to circuits.
2. **Rejects the romanticized "design as interdisciplinary negotiation"**: equal negotiation yields committee design — bloated, mediocre, nobody dares say no.
3. **A System Architect must be authorized**: someone with a clear vision of the system, acting as user's agent/approver/advocate.
4. **The user interface must be controlled by one person**: "If an architect cannot control it, a user certainly cannot."
5. **Don't believe in fantasy collaboration**: every part of a design has exactly one owner at any moment; meetings are "review + synchronization," not collective design.
6. **Small teams keep integrity**: Brooks studies collaboration models — architect + master builder (Wren), the surgical team, the chief programmer team — and the pattern is always one person's coherent concept executed with help, never joint design by committee.

### 2.1 Conceptual Integrity in Code (operational translation)

Brooks argues the case at the organizational level; the code-level translation of "one concept, consistently applied" is:

- (a) Every new module's interface must be checked against the Design Concept: if it does not fit, either the module is wrong or the concept needs updating — never both silently.
- (b) Test the design with 01's "deep module" lens: does the interface expose details that should stay hidden?
- (c) Test with 03's ETC: is this design easy to change when the concept evolves?
- (d) The UI (or API surface) is controlled by one coherent vision: when two parts of the system use the same concept with different shapes, that is a conceptual-integrity defect, not a local style choice.

**Application**: in multi-person work, designate one architect with full authority over the overall behavior model; beware of "let's all discuss and decide" meetings — that is review; always return to the Design Concept: "Is this change consistent with the core concept?"

---

## 3. Design Perspectives: A Set of Sharp Insights

> Navigation: nine heterogeneous insights, grouped by use — **constraints & resources** (§3.3–3.4), **validation & correction** (§3.1–3.2, §3.7, §3.9), **design craft** (§3.5–3.6, §3.8).

### 3.1 Rationalism vs Empiricism
Brooks is a firm empiricist: people err, so methodology must include early prototypes, user testing, incremental implementation, regression testing. **Don't assume "if I specify it well enough it will be right" — verify empirically.**

### 3.2 User Models — Better Wrong than Vague
> **"An articulated guess beats an unspoken assumption."**

The hidden trap: everyone carries their own user model in their head, nobody writes it down, and each makes different micro-decisions. **Guess explicitly**: write down "who the users are, how they use it, frequency distribution" and have the team debate and correct it. OS/360 counter-example: the debug module carried two inconsistent philosophies (batch vs terminal) because no one consciously decided to support both.

### 3.3 Budgeted Resources (Inches, Ounces, Bits, Dollars)
> **"Name the scarce resource explicitly, track it publicly, control it firmly."**

Every design has one critical scarce resource: coastline feet for a seaside house, bits in an instruction format, days in Y2K, disk accesses in OS/360. **Pick one, track it publicly, let one person decide allocation** — e.g. memory footprint, network bandwidth, wall-clock latency.

### 3.4 Constraints Are Friends
> **"Form is liberating."**

Constraints shrink the search space, which liberates creativity (Bach's forms, Michelangelo's flawed marble). But first distinguish four kinds: **true constraints / obsolete constraints (once true) / imagined constraints (the nine-dots problem) / deliberately artificial ones**. General-purpose products are much harder to design than special-purpose ones — fewer constraints guide the design. Stuck on a perceived limit? Maybe change the tool rather than contorting the design. (Thomas & Hunt's "find the box" in 03 Tip 81 is the practical companion: identify which of the four kinds your constraint really is.)

### 3.5 Aesthetics and Style
Dimensions of logical beauty: **Parsimony** (great things from few elements), **Structural Clarity** (a direct path from what you mean to how you say it), **Metaphor** (the Mac desktop), **Consistency** (knowing part lets you predict the rest). From consistency derive three principles: **Orthogonality** (independent features don't affect each other), **Propriety** (don't introduce things irrelevant to the purpose), **Generality** (core mechanisms apply to multiple situations). (01 §5.3 applies the same Consistency at the code/API level; 03 Tip 17 gives the operational test: "when you change A, must you also change B?")

### 3.6 Exemplars
Great designers study precedents deeply (Palladio measured Roman ruins; Bach walked 250 miles to study Buxtehude). Two traps: **laziness** (copy one and tweak → mediocrity) and **originality for its own sake** (aiming at "making something new" yields only novelty; aiming at "making something truly useful" yields durable value). The design records and histories of the systems you most admire are the material most worth reading.

### 3.7 How Expert Designers Err
> **"The besetting mistake of expert designers is not designing the thing wrong, but designing the wrong thing."**

Amateurs make small errors; experts make big ones — building the wrong thing. Bridges collapse roughly every generation because each generation grows bolder with new materials and forgets the underlying assumptions. JCL is the classic case: designed from previous-generation experience, never realizing it was a programming language. **Beware overconfidence after success; every six months ask "are my assumptions about the environment, the users, and the technology still valid?"**

### 3.8 The Divorce of Design
Designers grow ever farther from users and implementers (Edison built his own inventions; today nobody can build their own chip). Prescriptions: **use your own product** (builder as user), incremental delivery ("Can I have a chair?" kills a whole design assumption in one sentence), concurrent engineering (design and implementation proceed in parallel, not in sequence), and an architect-implementer split that keeps the architect close enough to implementation reality to be contradicted by it.

### 3.9 Trajectories and Rationales
The Compendium experiment to reconstruct design logs failed, but revealed: **design does not satisfy needs discovered up front — it discovers needs; it does not choose among alternatives — it becomes aware that alternatives exist**. Keep logs of *why* you designed this way — when requirements shift or the world moves on, the rationale is the only material that tells you whether the underlying assumptions have died.

---

## 4. Process vs Greatness (compressed)

SEI's CMM holds that "good process produces good products" — **Brooks says outright: nonsense**. Products with fan clubs (Apple II, Mac, Unix, iPhone) were almost all made outside the normal product process. Process is conservative, predictability-obsessed, fights the last war, is veto-oriented (every gatekeeper is paid to stop errors, not to enable greatness), and consensus mechanisms sand off the sharp edges — where the edge is.

**What process is good for: it raises the floor.** Pulls the low end up to average; the ceiling remains talent. Prescriptions that survive translation to engineering practice: **put rails only around the critical things, not high walls around everything; keep a fast exception path** (one request + one senior approval bypasses the rails); **delegate design to a chief designer and protect them from second-guessing and interruption**. The management machinery around this (critiqued practice, recruiting for brilliance, dual-track promotion) is organizational, not architectural — noted here only because it explains why the ceiling is a people problem, not a process problem.

---

## 5. Five Principles Most Worth Taking Away

1. **Conceptual integrity above all** — one clear Design Concept shared by the whole team; every micro-decision returns to it (code-level checklist: §2.1)
2. **Authorize one chief architect and protect their flow** — design is not democracy
3. **Don't believe in Waterfall, but understand why it exists** — Spiral with explicit contracting points
4. **Constraints are friends; special-purpose is easier than general** — distinguish true constraints from imagined ones
5. **"Are you building the right thing?"** — the question experts forget most; when the project is going smoothly and confidence is high, stop and ask whether the underlying assumptions have already failed

---

## 6. Relationship to the Skill Architecture

- **Conceptual integrity** → direct support for the Core Principle (code as executable projection of the conceptual model)
- **Explicit user models / rationales** → Brooks-side corroboration of heuristics ④ (explicit over implicit) and ② (projection fidelity)
- **Constraints are friends / budgeted resources** → philosophical basis for heuristic ⑦ (isolate points of change)
- **Process vs greatness** → echoes "high cohesion / low coupling is a verification standard, not a construction principle": principles are rails, not scaffolding

**See also**: 01 §3.1 Deep Modules (conceptual integrity in interface terms) · 01 §5.3 Consistency (code-level consistency) · 03 Tip 14 ETC (changeability as the master test) · 03 Tip 17 Orthogonality (derived from Brooks' Consistency) · 03 Tip 81 Find the box (constraint classification companion) · 05 §1.3 Ubiquitous Language (the shared concept made explicit)
