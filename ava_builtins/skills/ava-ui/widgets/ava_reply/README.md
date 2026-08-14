# ava_reply — page → agent callback spine

The shared helper the interactive widgets (`choice`, `confirm`, `form`,
`compare`) use to send their result back to the agent. A display-only page
doesn't need it.

## The contract

When you write the page, template three values into `reply.js` (you know all
three at write time):

| placeholder | fill with | what it is |
|---|---|---|
| `__GATEWAY_URL__` | `ava.GATEWAY_URL` | the gateway base, e.g. `http://100.x.y.z:8800` |
| `__AGENT_ID__` | `ava.self.AGENT_ID` | the agent to wake — usually yourself; a peer's id to report to it |
| `__PAGE_NAME__` | the `name` you passed to `ava.ui.show` | tags the inbound `source` as `ui:page:<name>` |

Then call `avaReply("...")` on submit. The string lands as an inbound on the
target agent, which wakes and reads it.

## Flow

1. You build the page (a widget below), `ava.ui.show(name, port)`.
2. The user opens the page (from the chat Pages popover or the `/fleet` per-row
   panel button), interacts, submits.
3. The page POSTs `avaReply(...)` → the gateway delivers it as an inbound → you
   wake with the result and act.

## Security

This rides the unauthenticated, CORS-open `POST /api/agents/{id}/messages`
endpoint. That is deliberate — the private network / single machine is the trust
boundary, and a page can do nothing the agent itself couldn't (see the ui skill
"Sending a decision back from the page" and `conventions/non-goals.md` "Auth /
multi-user"). Do not treat it as an authorization check.
