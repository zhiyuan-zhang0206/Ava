---
type: doc
title: Guarded page retry surfaces
description: Versioned page registry acceptance and observed-row close routes.
---

# Guarded page retry surfaces

| Method and route | Retry policy | Transactional acceptance owner |
|---|---|---|
| `POST /api/keyed/v1/agents/{agent_id}/pages` | keyed | replacement registry row and original PageRow receipt |
| `POST /api/keyed/v1/agents/{agent_id}/pages/{name}/close` | keyed | observed numeric row close and original PageRow receipt |

These are the only page register and close write surfaces; they require
verified `principal-v1` and a valid caller key. SDK
`ava.gateway_client.register_page` sends one fresh key per call and retries an
ambiguous outcome under it. `close_page` first reads the agent's open pages, then closes
exactly the observed row; no open page of that name means nothing is sent.
Fresh registration allocates a new row ID even after an expired same-name
registration. Historical acceptance is not proof that the name-based URL still
serves the original page. See the current domain owner for transaction,
stale-target and unchanged-close requirements:
[[gateway/routers/docs/page-acceptance.ava.okf.md]].
