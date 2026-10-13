"""The retry policy of web reads."""

from __future__ import annotations

from base.host.net.resilience import ExponentialBackoff, Policy, http_classifier

__all__ = ["WEB_READ_POLICY"]

# Web reads are idempotent — retry transient failures (429/5xx/transport)
# with exponential backoff + Retry-After respect (the audit-06 Q1 gap).
WEB_READ_POLICY = Policy(
    max_attempts=3,
    backoff=ExponentialBackoff(base=1.0, factor=2.0, cap=8.0),
    classify=http_classifier,
    respect_retry_after=True,
)
