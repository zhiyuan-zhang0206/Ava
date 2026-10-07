# Independent inference service IDs and exact effort

The user approved enabling CodeAct batching by default and exposing Fast as
an independent Ava LLM ID. The model picker should show vendor reference TPS,
and unsupported effort grades should fail instead of mapping to nearby grades.

## Service identity

An Ava ID identifies an inference service with its own price and throughput
facts. A Fast ID can share a provider's wire model with Standard while its
builder selects `service_tier=fast` (OpenAI) or `speed=fast` (Anthropic).
MiMo UltraSpeed already has a separate vendor model ID and keeps it.
No extra Fast toggle is added to the model picker or agent configuration.

Fast entries inherit architecture, context, media support and effort options
from their Standard owner. They declare their own rates, supersession chain
and reference TPS. A throughput figure never transfers between services.
The pricing updater reads these declarations without executing plugin code.

The provider's actual service receipt determines usage and billing. A Fast
request served at Standard uses Standard rates. Missing or unknown receipts
fail before quoting a price. The requested service ID remains in usage events.

## Cache and speed evidence

[Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
explicitly invalidates system and message caches when speed changes, while
tool definitions remain reusable. [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
describes prefix matching and routing but does not promise sharing between
Standard and Fast. Independent service IDs do not assert that every provider
always loses every cache layer; they avoid assuming cache equivalence.

[OpenAI Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)
documents actual service-tier receipts and downgrades. [Claude Fast mode](https://platform.claude.com/docs/en/build-with-claude/fast-mode)
documents speed receipts, beta access and output throughput improvements.
The existing pinned stable SDKs support these request shapes; no dependency
upgrade is required.

## Picker and effort

Reference TPS is populated only from reliable official absolute figures and
retains its source, verification date and measurement qualifier. For example,
[OpenAI's published Fast SLA](https://openai.com/api-fast-mode/) qualifies the
legacy GPT-5.6 thresholds; relative multipliers cannot establish absolute TPS
for current GPT-6 or MiMo models. Missing figures display a dash.

Model-declared effort options are authoritative in the picker, spawn boundary
and providers. Graded effort passes through unchanged or fails. Native binary
thinking switches and the historical media SDK default retain their existing
conversion because they express different contracts from graded effort.

## CodeAct batching

The batching prompt defaults on and remains explicitly configurable off.
This supersedes the default-off discussion in the 2026-08-26 prompt history;
it does not change the existing `execute_code` tool surface.
