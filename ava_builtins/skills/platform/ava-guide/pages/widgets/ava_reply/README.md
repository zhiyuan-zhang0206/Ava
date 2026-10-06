# Page replies

The shared `reply.js` helper submits the user's input from an Ava page. The
choice, confirm, form, and compare widgets embed the same body for zero-build use.
A display-only page does not need it.

## Prepare a reply

Template `__AGENT_ID__` with the owning agent's `ava.self.AGENT_ID`, and
`__PAGE_NAME__` with the registered page name. These identify the destination
and label the input; they are not credentials. Describe the exact question and
what each choice means before asking the user to submit it.

Call `avaReply(content)` when the user submits. Await success before displaying
"Sent"; a failed request should keep the user's input available for retry.
The result arrives as ordinary user input and wakes the destination agent.
A label such as "approved" authorizes only the specific action presented, not
unrelated operations or access to other agents.

## Existing browser transport

Open the returned Ava page URL in the browser where the user is signed in.
The helper uses a relative message endpoint and the existing same-origin login
session. Do not insert a gateway URL, bearer token, machine secret, or agent
credentials into page files. There is no separate page token or frontend package.

Opening the file directly, browsing the raw server port, or hosting it on an
unrelated origin does not provide this authentication context. Display components
remain reusable there; submitting to Ava requires an explicitly configured,
authenticated integration. Do not infer authorization from an agent id or the
`source` label. The current message API is a user message endpoint, not a
page-scoped permission system.

The platform message/authentication implementation owns the HTTP contract; see
`gateway/agents/` and `gateway/auth/` in a source checkout. The helper only
encapsulates the request used by these resources.
