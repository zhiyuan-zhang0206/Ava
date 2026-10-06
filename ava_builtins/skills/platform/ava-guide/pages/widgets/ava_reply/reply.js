// Submit user input from a page opened through Ava's authenticated page URL.
// The browser uses the existing same-origin login session. These identifiers
// route the input; they are not credentials and do not grant permissions.
// Template the owning agent id and the registered page name. Display-only
// pages do not need this helper. The widgets embed this body for zero-build use.

const AVA_AGENT_ID = "__AGENT_ID__";
const AVA_PAGE_NAME = "__PAGE_NAME__";

async function avaReply(content) {
  const res = await fetch(`/api/agents/${AVA_AGENT_ID}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, source: `ui:page:${AVA_PAGE_NAME}` }),
  });
  if (!res.ok) throw new Error(`avaReply failed: HTTP ${res.status}`);
  return res.json();
}
