# Gateway typed error envelope

Gateway JSON errors now use one `application/problem+json` envelope. The
previous endpoint-specific `detail` shapes forced every caller to discover
exception handling independently and did not carry retry or correlation data.

The common fields are `type`, `code`, `status`, `detail`, `retryable`, and
`trace_id`. `detail` remains a string for frontend compatibility. The builder
uses the active OTel trace ID when available and otherwise the request ID bound
by gateway middleware. `reason` remains an AvaAgentError-only field so the SDK
can reconstruct its existing exception classes.

| Error class | Code | Retryable |
|---|---|---|
| AvaAgentError | `reason.value` | no |
| Loki query budget | `loki_query_budget_unavailable` | yes |
| Observability read isolation | `observability_read_unavailable` | yes |
| Prometheus query budget | `prom_query_budget_unavailable` | yes |
| HTTPException | `http_<status>` | only 429 and 503 |
| Request validation | `validation_error` | no |
| Unhandled exception | `internal_error` | no |
| Gateway middleware and direct responses | stable route-specific code | as declared by the response |

FastAPI validation keeps its structured list in the `errors` extension while
using the short `detail` string `Request validation failed`. Existing statuses
and `Retry-After` headers remain unchanged. The health endpoint's 503 identity
payload is intentionally not an error envelope.
