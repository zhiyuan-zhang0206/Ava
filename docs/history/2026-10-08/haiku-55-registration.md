# Register Haiku 5.5

The user requested Haiku 5.5 on 2026-10-08. Register it in the Anthropic
provider contribution and preserve Haiku 4.5 as a selectable predecessor;
existing agent configurations must not silently change models.

Use the [official overview](https://platform.claude.com/docs/en/models/haiku-5-5/overview)
and [migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide)
checked on this date. Copying the predecessor would retain manual thinking
and miss adaptive effort. Reusing another Claude model's flat price would
miss the whole-request price change above 100K input tokens, including
cache reads and writes. The provider owns these values; the pricing archive
retains the corresponding provenance and periods.

Registration does not select this model for existing agents or deploy code.
No paid API benchmark or live provider request was needed for registration.
Targeted contracts cover catalog discovery, request construction, picker
metadata, cache-write billing and both sides of the price boundary.
