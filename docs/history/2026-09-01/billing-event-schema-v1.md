# Ava billing event schema v1

## Decision

Every completed provider LLM call emits one separate `ava.billing.call` OTel
span. The v1 ledger attributes are centralized in `shared/lm/billing.py` and
use the reviewed pricing catalog rather than provider-reported fees.

The billing span remains separate from Traceloop's `gen_ai` spans. It parents
under the current OTel context, records the provider-call start when the caller
measured it, and ends immediately so it remains a stable ledger record without
changing the traced provider instrumentation.

## Consequences

- Calls without LangChain usage metadata or a known manufacturer prefix emit no
  billing event, preserving the distinction between unknown usage and zero use.
- Models absent from the pricing catalog emit `cost=0.0` and `unpriced=true`;
  catalog prices remain the sole accounting authority.
- All trace resources carry `service.line=ava` and a production or development
  environment identity, allowing billing spans to be grouped consistently with
  the rest of the process trace.
