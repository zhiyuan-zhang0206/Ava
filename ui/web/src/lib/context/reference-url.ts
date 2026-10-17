export const REFERENCE_PARAM = "reference";

// meiva:ref ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md#panel-navigation-history
export function readReferences(url: URL, agentId: number): string[] {
  if (Number(url.searchParams.get("agent_id")) !== agentId) return [];
  return url.searchParams.getAll(REFERENCE_PARAM);
}

export function withReferences(href: string, references: string[]): string {
  const url = new URL(href);
  url.searchParams.delete(REFERENCE_PARAM);
  for (const reference of references) url.searchParams.append(REFERENCE_PARAM, reference);
  return url.toString();
}
