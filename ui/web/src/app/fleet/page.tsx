// /fleet — full-screen multi-agent supervision view. Renders the live agent
// relationship graph (spawn/fork/resurrect lineage + decaying message traffic)
// with each agent's self-reported activity, so a human can judge many agents at
// once without opening any single conversation. See components/fleet/fleet-view.tsx.
//
// FleetView reads the shared ["agents"] cache (useFleetAgents), kept live by
// The R4 fold owner (inside EventStreamProvider) — the single writer subscribed to the global /api/system
// broadcast provided once at the app root (components/providers.tsx) — so it
// stays fresh across page navigation without the fleet view running its own SSE
// merge.

import { ErrorBoundary } from "@/components/error-boundary";
import { FleetView } from "@/components/fleet/fleet-view";
import { FLEX, FLEX_1, MIN_H_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function FleetPage() {
  return (
    // <main> landmark (Task #1051 a11y) — the fleet page is a primary
    // surface; same pattern as app/page.tsx.
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0)}>
      <ErrorBoundary>
        <FleetView />
      </ErrorBoundary>
    </main>
  );
}
