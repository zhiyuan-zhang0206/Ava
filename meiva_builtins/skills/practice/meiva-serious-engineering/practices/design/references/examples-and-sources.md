# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **Reinventing the Commodity Layer**: re-deriving deployment, orchestration, versioning, or supervision from first principles, sized to an idealized end state, and exercised only through stubs. The alternative is to start from the established practice and the simplest sufficient design, run it on the real topology, and add a mechanism only for a named failure.
- **First-Idea Commitment**: picking the first solution that comes to mind and running with it → alternative: always produce and compare at least two alternatives before committing.
- **Unspoken User**: everyone assumes they know the user, nobody writes it down → alternative: write an explicit user model; a wrong model gets corrected; an absent model never does.
- **Constraint Creep**: treating every historical accident as an unchangeable constraint → alternative: classify every constraint; imagined and obsolete constraints are fair game to challenge.
- **Decision Without Rationale**: the team knows what was chosen but not why → alternative: every significant decision gets an ADR with context, alternatives, and consequences.
- **Shallow Interface Design**: an interface that exposes implementation details and forces callers to understand internals → alternative: apply the "deep module" test — count what callers must know; if >3–4 items, push complexity down.
- **The Silent General Hook**: a global idempotency/caching/retry hook that intercepts the core path and swallows work when a key is missing (null `external_id` → second order dropped, no error). → alternative: isolate the hook at an explicit opt-in entry; define and test missing-key semantics.

## Examples

### Bad → Good: User Model

**Bad** (implicit, vague):
> "The system should let users manage their data."

**Good** (explicit, falsifiable):
> **User Model — Data Analyst (primary)**: Works with CSV exports weekly. Knows spreadsheet formulas but not SQL. Needs: upload a file, see a summary, filter by date range, export filtered results. Does NOT need: raw database access, schema editing, collaboration. **Constraint**: must work on a 13" laptop screen at 150% zoom.

### Bad → Good: Design Rationale

**Bad** (decision without why):
> "We used PostgreSQL for the analytics store."

**Good** (ADR):
```markdown
# ADR-003: PostgreSQL for analytics store

**Context**: analytics queries need window functions, CTEs,
and JOINs across 5+ tables. Current MySQL store cannot
support these. Peak load ~50 QPS, 10GB data/year.

**Decision**: PostgreSQL 17 with TimescaleDB extension.

**Consequences**:
- Easier: window functions, lateral joins, materialized views
- Harder: operational knowledge (team knows MySQL), backup
  tooling needs updating, one more DB type in the stack

**Alternatives considered**:
- ClickHouse: faster for columnar scans but poor JOIN support
  and another operational surface
- Stay on MySQL + denormalize: simpler operationally but
  denormalized tables would drift from source of truth
```

### Bad → Good: Interface Design

**Bad** (shallow — caller must know too much):
```python
def process_payment(
    amount: float,
    currency: str,
    gateway: str,
    retry_count: int,
    timeout_ms: int,
    idempotency_key: str,
    webhook_url: str,
) -> PaymentResult: ...
```

**Good** (deep — complexity hidden behind the interface):
```python
def process_payment(
    request: PaymentRequest,
) -> PaymentResult:
    """Charge the amount. Retry with exponential backoff
    on transient failures. Idempotent: resubmitting the
    same request returns the original result."""
```
The retry policy, timeout strategy, idempotency key generation, and webhook delivery are all handled inside the module — the caller only expresses intent.

## Sources

- Ousterhout, *A Philosophy of Software Design* — design-it-twice, deep modules, pull-complexity-down, write-comments-first (§4.1, §4.4, §5.2)
- Brooks, *The Design of Design* — user models (§3.2), constraints as friends (§3.4), design rationales and trajectories (§3.9), Spiral model (§1.4)
- Thomas & Hunt, *The Pragmatic Programmer* — "find the box" constraint classification (Tip 81), tracer bullets as a design-validation tool (Tip 20)
- 45ck/software-architecture-skills — ADR format and decision-log discipline
- **Layer-1 behavioral eval (2026-08-06)** — t4: a general idempotency hook silently swallowed orders on external_id:null(`research/eval/ab/judge-verdict-t4.md`)
- **Unified cluster lifecycle (2026-09-24 to 2026-09-30)** — a release system sized to an idealized end state; its networked path could not run on the real topology, and a scripted stop/checkout/sync/start replaced it (`docs/postmortems/0009-complexity-must-name-the-failure-it-prevents.md`)
