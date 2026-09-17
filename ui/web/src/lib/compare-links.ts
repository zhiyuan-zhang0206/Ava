// Route helper for the multi-agent run-timeline compare view. The literal
// return type is load-bearing (like fleetHref's): it lets next/link and
// useRouter().push infer the typed-route arm without a cast.

/** The compare route with its preselected lanes. One id opens the selector
 *  prefilled; 2–3 ids open the side-by-side view. */
export function compareHref(agentIds: number[]): `/insights/compare?agents=${string}` {
  return `/insights/compare?agents=${agentIds.join(",")}`;
}
