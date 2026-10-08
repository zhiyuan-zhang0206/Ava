# Insights read models are their own service, behind a gateway proxy

## Context

The run timeline was a set of gateway routes. A cold read rebuilds an agent's stitched
history from its checkpoints and derives units, usage sums and token counts from it: about
three seconds of pure-Python CPU for a long-lived agent. The routes are synchronous `def`
handlers, so they ran in the gateway's anyio thread pool and held the GIL while they did.
Concurrent timeline reads therefore consumed pool slots every other synchronous route
needs, and slowed the gateway's event loop (SSE streams, the health probe, auth) through
GIL contention. More read models of the same kind (cross-agent cluster insights) are
coming, all CPU-bound and cache-heavy.

## Decision

`services/derived/insights` is a root unit of its own. It owns every insights read route
and the checkpoint-history cache (`HistoryViewCache`); the gateway keeps no copy. It serves
HTTP with uvicorn on a Unix socket (`$AVA_HOME/run/insights.sock`, mode 0600). It has no
port and no fixed-table slot: `/healthz` answers on the same socket and the roster entry's
identity probe asks it there (this service, this home, the pid its pidfile records), the
shape `browser-mcp` already has.

- The browser dials only the gateway. `/api/agents/{id}/run-timeline*` and
  `/api/insights/*` keep their URLs; `gateway/routers/insights.py` forwards them over the
  socket. Authentication, the pause policy and the eval-isolation check stay in the gateway;
  the service has no auth because nothing but the gateway can reach it.
- The gateway declares each typed route with the service's query parameters and response
  model, so the OpenAPI contract and the generated frontend types are unchanged.
- What the service needs from the gateway moves down to `base` rather than the service
  importing `gateway` (which would reach the agent kernel and break the `services` import
  contract): the audit-row reads, the context breakdown and its response, and the agent
  model-overrides lookup.
- A stopped service is a 502 on those routes and nothing else; the gateway's own health
  does not depend on it.

## Alternatives rejected

- **Keep the routes in the gateway, move the build to `asyncio.to_thread` or a process
  pool.** The thread variant is what already happens and is what contends for the GIL. A
  process pool inside the gateway reinvents a service without supervision, a health
  endpoint or a restart story, and each worker would hold its own cache.
- **A TCP port with a second fixed-table slot.** A loopback port is reachable by every
  local user and the service has no auth; a 0600 socket confines it to the home's owner. Any port
  slot, health or API, would also change the closed port table, which every existing
  home's start intent must match: a hand edit of `start-intent.json` per home before the
  first start of the new code. The socket needs no slot, so the upgrade needs no step.
- **The browser dialling the service.** Two origins, two auth surfaces, CORS; the
  gateway proxy already is the precedent (Grafana, agent pages).
- **Streaming the proxied response.** The service answers with one finite JSON document;
  streaming would add nothing over buffering it.
- **Importing `gateway` modules from the service.** The `services` contract forbids the
  chain to the agent kernel; moving the shared primitives to `base` is the stated remedy.

## Consequences

- No port-table change and no manual upgrade step: an existing home starts the new code as is and
  the roster launches the service.
- Binding a Unix socket limits the home path length (about 100 bytes on macOS).
- One more process per gateway host, holding up to six agents' histories in memory instead
  of the gateway holding them.
- Routes added to the service must also be declared in the gateway proxy to appear in the
  OpenAPI contract; `/api/insights/{rest}` forwards undeclared ones untyped.
