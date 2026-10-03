# Concurrency and retry bounds

## Decision

SDK batch entry points default to twelve in-flight workers. The limit protects
the omitted-argument path while preserving an explicit caller-selected positive
limit.

The LLM retry policy has a 420-second configurable wall-clock budget. It stops
before another retry attempt once the elapsed sequence reaches the budget and
exports the final retry duration as `llm_retry.duration_seconds`. An in-flight
stream remains governed by its existing stream timeouts; the retry budget does
not cancel a healthy call mid-stream.

Silent-idle continuation is bounded by cumulative output tokens rather than a
turn count. The default 2,048-output-token ceiling halts before another call;
each `silent_idle` event reports the current output-token estimate from the
reviewed pricing catalog.

DeepSeek receives a default process limiter of 31 concurrent calls. That is
the floor of the 500-slot Pro account allowance divided across the default
16-concurrent-turn host budget. Sustained HTTP 429s now alert in Grafana as a
provider-grouped burst (>5 in five minutes), while a one-off 429 remains a
retryable signal.

## Rationale

The four bounds control independent multiplication effects: public batch
fan-out, retry time, reasoning-only loops, and account-wide provider pressure.
The chosen defaults leave a small DeepSeek account headroom (496 of 500 slots)
and keep the existing retry/de-phasing strategy intact.

## Update 2026-09-02

Some provider-compatible reasoning streams carry `reasoning_content` while
reporting zero output tokens. To retain the output-budget design without an
unbounded zero-token loop, each detected silent idle consumes at least one
budget token; its event cost continues to use the provider-reported output
tokens.
