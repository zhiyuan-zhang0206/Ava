# Versioned model pricing catalog with reviewed source updates

## Context

Ava estimates LLM spend when a call completes and stores the rates and cost on
the usage event. The model registry previously carried one three-number tuple
per model. That representation cannot express published future price changes,
daily peak/off-peak windows, or token-volume tiers. It also has no freshness
signal: provider prices can change while every Ava test remains green.

The primary user is a cluster operator who needs current cost estimates without
watching every provider pricing page. They need deterministic billing semantics,
an audit trail for every rate, and an explicit unknown state instead of a guessed
or zero price. Ava supports direct providers whose source capabilities differ:
xAI exposes a machine-readable price catalog, while OpenAI, Anthropic, Gemini,
DeepSeek, Mistral, and Groq publish prices only in documentation. Account cost
APIs report historical spend and are not next-call unit-price catalogs.

Provider pages and APIs are external, mutable dependencies. A transient response,
parser regression, unit-conversion mistake, or compromised source must not change
the price used by running agents without review. Agent runners can also live on
different machines, so runtime fetching would make one cluster disagree with
itself during an upstream outage or rollout.

## Decision

Pricing is a versioned, checked-in catalog separate from the model roster. Each
entry carries effective time bounds, rates, source provenance, and optional
selection conditions such as UTC time windows or input-token tiers. The pricing
module exposes `rates_at()` and `quote()` as the domain interface; callers do not
inspect catalog storage. A quote returns the cost and the exact rates chosen in
one operation so usage-event snapshots cannot straddle a schedule boundary.

Provider-specific source adapters fetch official catalogs or documentation,
normalize units, validate coverage and invariants, and propose a repository
change. Scheduled automation never mutates a running cluster's catalog directly.
The normal PR gate reviews and tests the generated diff before it reaches `main`.
Published future rates may be checked in ahead of time and activate at their
effective timestamp without another deployment.

The first complete adapter is DeepSeek because its August 2026 change combines
an effective date with two recurring UTC peak windows. Providers are added behind
the same adapter contract; actual-cost APIs remain a separate reconciliation
signal and never become unit-price input.

## Alternatives rejected

### Fetch prices in every running cluster

This applies changes without a deployment, but lets mutable web content enter the
billing path without review. It also creates cross-machine inconsistency, adds a
network dependency to cost observation, and makes last-known-good and parser
rollbacks operational runtime concerns. The lower update latency is not worth a
non-deterministic accounting boundary.

### Keep tuples in `registry.py` and have a bot rewrite Python

This produces a smaller initial diff, but the tuple still cannot represent time
windows, effective dates, tiers, currency, or provenance. Rewriting source code
with provider-specific regular expressions is also a shallow interface: every
new pricing shape leaks into the updater and its callers.

### Trust one third-party aggregate price API

Aggregators are useful drift detectors, but their route, region, markup, cache,
and long-context semantics do not necessarily match Ava's direct provider
accounts. In particular, OpenRouter pricing is authoritative only for the
OpenRouter billing channel. An aggregate cannot replace channel-specific official
sources.

## Consequences

Price selection becomes deterministic and testable at schedule boundaries, and
every usage event can retain an immutable quote even after later catalog changes.
Operators get automatic drift discovery without making external pages part of
the runtime critical path. The catalog is also able to encode already-published
future changes.

Each documentation-only provider needs a small, strict parser that will require
maintenance when its page layout changes. A parser failure keeps the reviewed
catalog in force and makes the update workflow fail visibly; it never interprets
missing data as free usage. Price changes still wait for the repository's CI and
review latency. Historical events written before usage-time snapshots remain
estimates unless their event timestamp and an applicable historical catalog
interval are both available.
