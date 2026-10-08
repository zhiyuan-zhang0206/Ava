# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns
- **Translation Tax**: business says "order," code says `PurchaseRecord`, docs say "transaction" — every conversation requires mental mapping. → **Alternative**: pick one term, align everyone, rename code and docs to match.
- **Thesaurus Code**: the same concept named differently in different modules (`UserManager`, `AccountService`, `MemberController`) because each author preferred a different synonym. → **Alternative**: enforce one name per concept across the entire codebase; use the glossary as the authority.
- **Overloaded Term**: "complete" means "payment confirmed" to sales, "shipped" to the warehouse, and "reconciled" to finance — and the code has one `Order.complete()` method. → **Alternative**: split into three distinct named concepts with three distinct code representations (`PaymentConfirmed`, `OrderShipped`, `ReconciliationComplete`), each owned by its Bounded Context.
- **Analysis-Model Decoration**: a UML diagram or wiki page describing a model that the code does not reflect — the language exists only on paper. → **Alternative**: make the code the authoritative expression of the model; any model change that does not reach the code is waste (05 §1.4).
- **Forgotten Glossary**: a glossary was created once and never updated — it now contradicts the code. → **Alternative**: treat the glossary as a source file gated by the same PR process as code; stale glossary entries are bugs.

## Examples

**Bad**: Business says "ticket." Code has `Ticket`, `Task`, `WorkItem`, `Issue`. PM docs use "task." The DB table is `tickets` but the API returns `items`. Every onboarding takes two extra days of translation.

**Good**: Business and engineering agree on "Ticket" as the single term. Code has `Ticket` class, `tickets` DB table, `/tickets` API endpoint, `ticket_id` foreign keys, `test_ticket_lifecycle` test. The glossary entry reads: "Ticket — a customer-reported issue tracked to resolution. Owned by the Support Context. Not to be confused with InternalTask (an ops-internal work item)."

**Bad**: An `Order` class carries a `status` field whose values include `"complete"` — but "complete" means different things to different departments, and the single field silently conflates them. When the warehouse marks it complete, the finance team's reconciliation breaks because it assumed "complete" meant funds settled.

**Good**: The `Order` aggregate exposes three explicit status fields — `paymentStatus`, `shipmentStatus`, `reconciliationStatus` — each with its own value type and lifecycle. No one confuses "payment complete" with "shipment complete" because the language forces them apart.

## Sources
- Evans, *Domain-Driven Design* (2003), §1.3 Ubiquitous Language, §1.4 Model-Driven Design — references/05-domain-driven-design.md
- Vernon, *Implementing Domain-Driven Design* (2013), §2.1 Ubiquitous Language extension — references/04-implementing-ddd.md
- Thomas & Hunt, *The Pragmatic Programmer* (20th anniv. ed., 2019), Tips 74 (Naming), 80 (Project Glossary) — references/03-pragmatic-programmer.md
- Ousterhout, *A Philosophy of Software Design* (2018), §1.2 Cognitive Load — references/01-philosophy-of-software-design.md
