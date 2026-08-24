/** Ask the always-up Gate to project the persisted UI update snapshot.
 *
 * The SPA never renders or times a maintenance page. SSE and polling are only
 * hints that make the browser re-request its current URL through the Gate.
 */
let reloadRequested = false;
export const UI_UPDATE_QUERY_KEY = ["ui-update-state"] as const;

export function reloadThroughGate(
  reload: () => void = () => window.location.reload(),
): void {
  // The SSE hint and the snapshot poll can resolve in either order. They share
  // this page-lifetime latch so the race produces one Gate navigation, never a
  // reload storm. A successful navigation creates a fresh module instance.
  if (reloadRequested) return;
  reloadRequested = true;
  reload();
}

export type GateMaintenanceState = {
  status: "inactive" | "updating" | "invalid";
  generation: string | null;
};

export async function getGateMaintenanceState(): Promise<GateMaintenanceState> {
  const response = await fetch("/__ava/deploy-state", { cache: "no-store" });
  if (!response.ok) throw new Error(`Gate deploy-state returned HTTP ${response.status}`);
  const value = (await response.json()) as Partial<GateMaintenanceState>;
  if (
    value.status !== "inactive" &&
    value.status !== "updating" &&
    value.status !== "invalid"
  ) {
    throw new Error(`Gate deploy-state returned unknown status: ${String(value.status)}`);
  }
  if (value.generation !== null && typeof value.generation !== "string") {
    throw new Error("Gate deploy-state returned an invalid generation");
  }
  return { status: value.status, generation: value.generation };
}
