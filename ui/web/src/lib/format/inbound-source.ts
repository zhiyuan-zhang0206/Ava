// An inbound message's `source` tags who sent it (the envelope model in
// base/agents/messages/envelope.py). `external_agent:<subject>[:<instance>]` is an
// agent outside Ava that declared itself, distinct from the user.

export const EXTERNAL_AGENT_PREFIX = "external_agent:";

/** The declared sender of an `external_agent:<subject>[:<instance>]` source, as "subject" or "subject / instance". */
export function externalAgentName(source: string): string {
  const parts = source.slice(EXTERNAL_AGENT_PREFIX.length).split(":");
  return parts.length > 1 ? `${parts[0]} / ${parts[1]}` : parts[0];
}
