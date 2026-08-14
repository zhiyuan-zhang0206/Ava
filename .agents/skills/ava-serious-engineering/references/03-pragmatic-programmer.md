# 03 · The Pragmatic Programmer

**Authors**: David Thomas & Andrew Hunt (1st ed. 1999; 20th-anniversary ed. 2019)
**Position**: Not about a language or framework — about **how engineers think and work**. 99 Tips across 9 chapters.
**Review status**: ⏳ Detail level per user-approved 01 benchmark; content pending user review (2026-08-05)

---

## 0. One-Sentence Core

> Good design is easier to change (**ETC — Easier To Change**). Every principle — DRY, orthogonality, reversibility — exists to make code easier to change.

---

## 1. Two Overarching Theses

1. **Care About Your Craft**: nobody can force you to write good code, but if you don't care, writing badly is inevitable. Craft is a personal discipline, not a job requirement.
2. **Think! About Your Work**: don't copy-paste from a tutorial and call it done. Ask of every line: why this way? Is there a better one? Thinking is the part of engineering nobody else can do for you.

---

## 2. The Pragmatic Philosophy (Tips 3–13)

- **Tip 4 Provide options, don't make lame excuses**: don't say "I can't because X is down"; say "X is down, here are options A/B/C, I recommend B because…". The book's namesake story: a colleague's source code vanished and he shrugged "the cat ate my source code" — the backup was in his car, and the car was stolen. The lesson is twofold: **own the outcome, and always have a recovery path**. Excuses are a symptom of no options, which is a symptom of no backup plan.
- **Tips 5–7 Broken windows / Stone soup**: one piece of rotten code left unfixed invites rot everywhere — fix broken windows on sight (even a TODO). To drive change nobody supports, build a small working demo (stone soup) and let people add to it
- **Tip 8 Good enough software**: treat "how good" as a requirements discussion. Is going from 92% to 94% test pass rate worth two weeks? That is a product decision, not one for engineers to carry alone
- **Tip 9 Knowledge portfolio**: manage learning like an investment — diversify, invest regularly (a new language a year), manage risk (don't bet everything on one technology), buy low / sell high (get in early on promising new technologies)
- **Tip 10 Critically analyze**: on "X is better than Y", ask: what setting? what benchmark? who paid for the research?
- **Tips 11–13 Communication**: English is just another programming language; what you say and how you say it matter equally; docs live with the code

---

## 3. The Pragmatic Approach (Tips 14–24) — ETC and DRY

### 3.1 The Core Principles

- **Tip 14 ETC**: when unsure between two designs, ask "which will be easier to change later?"
- **Tips 15–16 DRY**: not "don't duplicate code" — **"every piece of knowledge must have a single, unambiguous, authoritative representation"** — business logic, config, docs, test cases are all knowledge. Anti-example: defining the same component's behavior in a config file, API docs, and spec — the three will drift apart. **Make reuse easy**, or people won't reuse
- **Tip 17 Orthogonality**: when you change A, must you also change B? If yes, orthogonality is lacking. In a system with many moving parts, planning, execution, state, and presentation should be orthogonal

### 3.2 Change and Learning

- **Tips 18–19 Reversibility**: there is no "final decision." Architecture decisions need undo room; don't chase fashion — ask "what current pain does this solve?"
- **Tip 20 Tracer bullets**: **get the whole pipeline working with the crudest implementation first** (even hardcoded steps), then strengthen each link along the working path. Unlike a prototype: tracer code stays and evolves into production; prototypes are thrown away
- **Tip 21 Prototypes to learn**: a prototype answers one specific question ("can latency reach 200ms?"), not "write usable code"; prototypes must be thrown away
- **Tip 22 Program close to the problem domain**: the domain's own concepts should be first-class in code; a sea of `process_data_v2` means you are not close to the domain

---

## 4. Basic Tools (Tips 25–35)

### 4.1 Engineering fundamentals (assumed known)

The following are prerequisites for any serious engineering work, not architecture principles per se — if you are not already doing them, fix this before worrying about design: **plain text for config/data/docs** (greppable, diffable, version-controllable — anti-example: policy stored in a DB BLOB with no review or rollback, Tip 25), **the shell** (GUIs limit you to what the designer imagined; the shell lets you compose anything, Tip 26), **version control for everything** — code, config, docs, schemas (Tip 28), and **a text-manipulation language** for bulk edits and log extraction (Tip 35).

### 4.2 The six iron laws of debugging (Tips 29–34)

1. **Fix the problem, not the blame**
2. **Don't panic** (the book's nod to *The Hitchhiker's Guide*): return to first principles — what is the code supposed to do, what is it doing, what is different?
3. **Make the code fail in a test before fixing it** — without a reproducible failure, you may not be fixing the bug at all
4. **Read the damn error message** — the stack trace usually tells you 90%
5. **"select" isn't broken** (the story: engineers spent weeks debugging an OS, blaming the `select()` system call — it was their own code): libraries, OSes, and compilers are usually right — suspect your own code first
6. **Don't assume, prove** — "I think this path is slow"? Look at the profiling data, look at the query plan — evidence, not guesses

---

## 5. Pragmatic Paranoia (Tips 36–43) — You Cannot Write Perfect Software

### 5.1 Design by Contract (Tip 37)

Every function defines preconditions (what the caller guarantees), postconditions (what it guarantees back), invariants. **When a caller violates the contract, refuse immediately — don't struggle through.**

### 5.2 Crash early and assert (Tips 38–39)

- **Tip 38 Crash early**: report errors the moment they happen; don't let them propagate. Anti-example: a tool returns schema-violating data, you silently stuff a default; half an hour later behavior is weird and you debug forever to find upstream pollution
- **Tip 39 Assertions guard the impossible**: that "impossible" branch — it will happen. Don't strip asserts

### 5.3 Balance resources (Tips 40–41)

Whoever opens closes, whoever allocates frees, keep scope small (with / RAII / defer).

### 5.4 Don't outrun your headlights (Tips 42–43)

Small steps, frequent feedback. Don't predict far ahead — design a changeable system rather than a predicted-correct one.

---

## 6. Bend or Break (Tips 44–55) — Coupling Is the Root of Rot

### 6.1 Decouple (Tips 44–47)

- **Tip 44 Decoupling makes change easier**: always ask "will I be forced to change B when I change A?"
- **Tip 45 Tell, Don't Ask**: don't `obj.getX().getY().doSomething()` — let obj expose `obj.doSomethingWithY()`
- **Tip 46 Law of Demeter**: don't chain `a.b().c().d()` — changing b's internals will break a wide swath
- **Tip 47 Avoid global data**: the "global state" temptation is huge — convenient short-term, debt long-term

### 6.2 Data over objects (Tips 49–50)

- **Tip 49 Programming is about code, programs are about data**: thinking about **data flow** is closer to the essence than "object relationships." Draw API request → DB row → message → response
- **Tip 50 Don't hoard state; pass it around**: let state travel explicitly between components, not hide in a mutable singleton

### 6.3 The inheritance tax (Tips 51–54)

Express polymorphism with interfaces (52), share with composition/delegation (53, has-a beats is-a), use mixins (54). Inheritance is the tightest coupling available — pay the tax only when it buys you something real.

### 6.4 Configuration (Tip 55)

Parameterize with external config — separate config from code, but don't over-configurify.

---

## 7. Concurrency (Tips 56–60) — Shared State Is Wrong State

- **Tip 57 Shared state is incorrect state**: any mutable state read/written by multiple execution flows is a bug magnet — the ordering of reads and writes becomes nondeterministic, the same input produces different outputs, bugs become unreproducible and untestable. Remedies: immutable data, message passing, actor model
- **Tip 58 Random failures are usually concurrency problems**: "fails 1 time in 10" — almost certainly a race condition
- **Tip 59 The actor model**: each actor owns its state, communicates only by message (Erlang/Elixir)
- **Tip 60 The blackboard**: independent workers write to a shared blackboard and respond when something interests them

---

## 8. While You Are Coding (Tips 61–74)

### 8.1 Coding habits (Tips 61–62)

- **Tip 61 Listen to your inner lizard**: when something feels off, stop — intuition is compressed experience. "I can't write this code" usually means the design is wrong
- **Tip 62 Don't program by coincidence**: after it works, **state precisely "this line changed X to Y, so Z no longer happens."** Understand every line you ship; code that works by accident is debt you cannot debug

### 8.2 Refactoring (Tip 65)

Refactor early, refactor often — refactoring is not "dedicate a week", by then it's too late. Fix rot on sight, a little at a time; never refactor without tests.

### 8.3 Testing as a design tool (Tips 66–71)

- **Testing is not about finding bugs — testing is a design tool**: hard-to-test code is poorly designed code
- **Tests are the first user of your code**: writing tests forces you to think "how will others use this API?"
- **Tip 68 Build end-to-end**: get the whole pipeline working from the start (echoes tracer bullets)
- **Tip 69 Design for testability**: dependency injection, pure functions, no globals
- **Tip 70 Test your software, or your users will** — pick one
- **Tip 71 Property-based testing**: define properties, not cases — e.g. for a sort: the output is sorted, its length equals the input's, and it contains exactly the input's elements. Properties catch what hand-written cases miss; extremely useful for nondeterministic systems

> **On TDD granularity (reconciling with 01 §7.1)**: Ousterhout criticizes TDD for driving single-feature passes at tiny granularity; this book praises testing as a design tool. Both are right — the reconciliation is to test at the **abstraction level**, not the feature level: unit tests that pin down a module's contract (preconditions/postconditions, Tip 37) rather than a feature's happy path. Property-based tests (Tip 71) that verify invariants across the model are one way to do exactly that.

### 8.4 Security (Tips 72–73)

Minimize attack surface, patch early. Systems that mix untrusted input and powerful actions are attack hotspots.

### 8.5 Naming (Tip 74)

`process()` is noise, `scoreCandidatesByRelevance()` is signal; rename when the name stops fitting.

---

## 9. Before the Project (Tips 75–83)

- **Tip 75 Nobody knows what they want**: the PM's doc is an approximation of the real need
- **Tip 77 Requirements are learned from feedback loops**: a week with a demo beats three weeks discussing spec
- **Tip 78 Work with a user**: watch 5 real users instead of reading 5 PRDs
- **Tip 79 Policy is metadata**: business policy ("VIP quota is 10×") should be configurable data, not hardcoded if-else
- **Tip 80 Use a project glossary**: what "user/customer/tenant" each mean — agreed by all
- **Tip 81 Don't think outside the box — find the box**: identify the real constraints; often a constraint turns out to be imagined (Brooks' four-way classification of constraints in 02 §3.4 is the companion analysis)
- **Tip 83 Agile is not a noun**: its core is "small steps, get feedback, adjust"

---

## 10. Pragmatic Projects (Tips 84–97)

### 10.1 Teams and process (Tips 84–88, compressed)

Small and stable teams (<10–12 people), schedule-based delivery (rhythm beats heroism), feature teams; no cargo cult — "Google does it so we should" is cargo cult, prove it works in your context first. Team practice belongs to project management; the architecture-relevant residue is: **delivery rhythm and feedback loops are design inputs**.

### 10.2 The pragmatic starter kit (Tips 89–95)

- 89 Drive build/test/release with VCS
- 90 Test early, test often, test automatically
- 91 Coding is complete only when all tests pass
- 92 Use a **saboteur** to prove tests can catch bugs — introduce a bug deliberately; the tests should fail, or your tests are decoration
- 93 **Measure state coverage, not line coverage** — 100% line coverage ≠ well-tested
- 94 **Find bugs once** — write a regression test after each fix
- 95 No manual procedures — if a release step is done by hand instead of in CI, that step will fail

### 10.3 Sign your work (Tip 97)

Take responsibility like a craftsman — your name on the code means you stand behind it.

---

## 11. Postscript (Tips 98–99)

- **Tip 98 Primum non nocere (First, do no harm)**: software affects real people; default to not causing harm
- **Tip 99 Don't enable evil**: if you know something is wrong, don't participate

---

## 12. The Seven Takeaways (when time is short)

1. **ETC (Tip 14)**: for every design trade-off ask "which is easier to change later?" — the master heuristic
2. **DRY (Tips 15–16)**: every piece of knowledge has one authoritative representation
3. **Orthogonality (Tip 17)**: changing A should not force changing B
4. **Contract + crash early (Tips 37–39)**: state expectations explicitly; fail the moment they are violated
5. **Tracer bullets (Tip 20)**: get end-to-end working first, then deepen each segment
6. **Shared state is wrong state (Tip 57)**: stay alert in concurrent / distributed settings
7. **Testing is a design tool (Tip 66)**: hard to test = badly designed

---

## 13. Relationship to the Skill Architecture

- **DRY / orthogonality** → heuristics ③ (single source of truth) + ⑦ (isolate points of change)
- **Design by contract / crash early / assertions** → direct source of heuristic ④ (explicit over implicit)
- **Tracer bullets / reversibility** → heuristic ⑧ (make deletion cheap and visible)
- **Naming / glossary** → heuristic ⑨ (naming is the contract), corroborating Evans' Ubiquitous Language (05 §1.3)
- **Testing as design tool / property-based** → heuristic ⑩ (tests anchor concepts); the granularity tension with Ousterhout's TDD critique is reconciled in 01 §7.1

**See also**: 01 §3.1 Deep Modules (orthogonality in interface terms) · 02 §3.4 Constraints Are Friends (Tip 81's "find the box" classified) · 02 §3.5 Consistency (orthogonality as a derived principle) · 05 §1.3 Ubiquitous Language (glossary as contract)
