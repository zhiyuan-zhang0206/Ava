// ava-reply — the spine every interactive widget uses to send the page's result
// back to the agent. The page's browser JS POSTs to the gateway's message
// endpoint, which delivers it to the target agent as an inbound; the agent went
// idle after ava.ui.show and wakes on this message.
//
// The agent templates these three values when it writes the page (it knows all
// three): __GATEWAY_URL__ -> ava.GATEWAY_URL, __AGENT_ID__ -> ava.self.AGENT_ID
// (the agent to wake — usually itself; another id to report to a peer),
// __PAGE_NAME__ -> the name you passed to ava.ui.show (used to tag the source).
//
// This rides the unauthenticated, CORS-open messages endpoint — fine because the
// private network is the trust boundary (see the ui skill "Sending a decision back from
// the page" + conventions/non-goals.md). It is NOT an auth check.
//
// The interactive widgets embed a copy of this inline (single-html = zero build,
// self-contained). This file is the canonical copy + the contract.

const AVA_GATEWAY_URL = "__GATEWAY_URL__";
const AVA_AGENT_ID = "__AGENT_ID__";
const AVA_PAGE_NAME = "__PAGE_NAME__";

// content: a string the agent will read as the inbound message. Send something
// readable (the agent is an LLM, it parses prose) — e.g. "choice: B" or a
// "field: value" block. Returns the delivery result; throws on a non-2xx.
async function avaReply(content) {
  const res = await fetch(`${AVA_GATEWAY_URL}/api/agents/${AVA_AGENT_ID}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, source: `ui:page:${AVA_PAGE_NAME}` }),
  });
  if (!res.ok) throw new Error(`avaReply failed: HTTP ${res.status}`);
  return res.json();
}
