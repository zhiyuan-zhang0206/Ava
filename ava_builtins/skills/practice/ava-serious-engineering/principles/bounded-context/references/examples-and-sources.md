# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns
- **God Context**: one Bounded Context absorbing every concept in the system — the "Enterprise" model with 500 entities that every team touches. → **Alternative**: split along language boundaries: when two sub-teams use the same word differently, they need separate contexts.
- **Context Sliced by Layer, Not by Language**: contexts split as "Frontend Context," "Backend Context," "Database Context" — the same domain concept appears in three places with three different representations. → **Alternative**: slice by business capability; each context owns its full vertical (UI, application logic, domain model, persistence) for one coherent set of business concepts.
- **ACL Skipped "Temporarily"**: the team calls the external API directly from domain logic, promising to add a translation layer later — later never comes. → **Alternative**: build the ACL first, even if it is thin; a one-method pass-through that exists is a seam you can thicken later; a direct call with no seam is permanent coupling.
- **Shared Kernel Abuse**: sharing a "common" library across contexts that grows until it becomes a de facto shared model — the worst of both worlds (tight coupling without the coherence of a single context). → **Alternative**: limit shared kernels to tiny, stable, coordinated pieces (value objects like `Money`, `EmailAddress`); never share Entities or Aggregates across contexts.
- **Conformist by Default**: every downstream context passively copies the upstream model without question — the system ossifies around one team's design choices. → **Alternative**: treat Conformist as a deliberate last resort; prefer Customer-Supplier (negotiate) or ACL (translate) whenever possible.

## Examples

**Bad**: An e-commerce system has one `Product` class with 150 fields — `name`, `imageUrl`, `seoDescription` (Catalog), `sku`, `warehouseLocation`, `safetyStock` (Inventory), `basePrice`, `promotionRules`, `taxCode` (Pricing). Every new feature touches this class. Changing the pricing model requires regression-testing the catalog UI. Nobody fully understands every field.

**Good**: Three separate contexts — Catalog (`Product` with name, images, specs, SEO), Inventory (`StockItem` with SKU, location, quantity, safety stock), Pricing (`PricedProduct` with pricing rules, promotions, tax) — each with its own model, its own database tables, linked by a shared product ID. Pricing changes never touch Catalog code. Inventory can switch warehouses independently. A Domain Event (`PriceChanged`) propagates updates across contexts when needed.

**Bad**: A payment service calls Stripe's API directly from its domain service — `stripe.Charge.create(...)` inside `PaymentService.processPayment()`. When Stripe upgrades its API version and renames fields, the domain logic breaks, and the fix touches core payment code.

**Good**: A `StripeACL` adapter sits between the domain and Stripe: it receives Stripe's `charge.succeeded` webhook, translates it into the domain's `PaymentReceived` event, and only then hands it to the domain service. When Stripe changes its API, only the ACL changes — the domain logic never knows.

## Sources
- Evans, *Domain-Driven Design* (2003), §4.1 Bounded Context, §4.2 Context Map, §4.3 Distillation — references/05-domain-driven-design.md
- Vernon, *Implementing Domain-Driven Design* (2013), §2.2 Bounded Context (implementation angle), §2.3 Subdomains and Core Domain, §2.4 Context Map — references/04-implementing-ddd.md
- Ousterhout, *A Philosophy of Software Design* (2018), §1.1 Complexity definition — domain complexity as a primary source — references/01-philosophy-of-software-design.md
