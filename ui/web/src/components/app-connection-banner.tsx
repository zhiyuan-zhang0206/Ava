"use client";

// Authenticated host-status polling and global SSE connection tracking.
import { useCallback } from "react";

import { useAuth } from "@/lib/auth-context";
import { useStore } from "@/lib/store";
import { useClusterHealth } from "@/lib/use-cluster-health";
import type { ConnectionEvent } from "@/lib/useEventStream";
import { useEventStream } from "@/lib/useEventStream";

export function AppConnectionBanner() {
  const { status } = useAuth();
  // Conditional MOUNT (not conditional hooks): the pollers + SSE subscription only
  // come alive once authenticated, so nothing 401s behind the login screen.
  if (status !== "authenticated") return null;
  return <ConnectionHealthProvider />;
}

function ConnectionHealthProvider() {
  const setConnState = useStore((s) => s.setConnState);

  // Poll cluster status + drive the reconnect-on-update-done edge. Single mount.
  useClusterHealth();

  // Track the GLOBAL broadcast's connection health and mirror it into the store
  // so the timeline's ConnectionNotice can read it.
  const onSystemEvent = useCallback(() => undefined, []);
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      if (ev.type === "parse-failed") return;
      setConnState(ev.type);
    },
    [setConnState],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  return null;
}
