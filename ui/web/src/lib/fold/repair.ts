import type { QueryClient, QueryKey } from "@tanstack/react-query";

const COALESCE_MS = 200;

interface Repair {
  key: QueryKey;
  dirty: boolean;
  running: boolean;
  timer: ReturnType<typeof setTimeout> | null;
}

/** Reconcile dirty read models without cancelling a repair already in flight.
 *
 * A hint during a read requires another read: the first snapshot may already
 * have been taken. Deadlines are never postponed by more hints, so continuous
 * activity cannot starve reconciliation. Settled keys leave the map.
 */
export function createQueryRepairScheduler(client: QueryClient) {
  const repairs = new Map<string, Repair>();
  let disposed = false;

  const run = async (id: string, repair: Repair): Promise<void> => {
    repair.timer = null;
    // An initial/independent fetch can predate this hint. Joining that read
    // saves a connection, but its result cannot discharge the repair itself.
    repair.dirty = client.getQueryCache().findAll({ queryKey: repair.key })
      .some((query) => query.state.fetchStatus === "fetching");
    repair.running = true;
    try {
      await client.invalidateQueries(
        { queryKey: repair.key },
        { cancelRefetch: false },
      );
    } finally {
      finish(id, repair);
    }
  };

  const finish = (id: string, repair: Repair): void => {
    repair.running = false;
    if (disposed) return;
    if (repair.dirty) {
      repair.timer = setTimeout(() => { void run(id, repair); }, COALESCE_MS);
    } else {
      repairs.delete(id);
    }
  };

  return {
    request(key: QueryKey, immediate = false): void {
      if (disposed || client.getQueryCache().findAll({ queryKey: key }).length === 0) return;
      const id = JSON.stringify(key);
      let repair = repairs.get(id);
      if (!repair) {
        repair = { key, dirty: false, running: false, timer: null };
        repairs.set(id, repair);
      }
      repair.dirty = true;
      if (repair.running) return;
      if (immediate) {
        if (repair.timer !== null) clearTimeout(repair.timer);
        void run(id, repair);
      } else {
        repair.timer ??= setTimeout(() => { void run(id, repair); }, COALESCE_MS);
      }
    },
    dispose(): void {
      disposed = true;
      for (const repair of repairs.values()) {
        if (repair.timer !== null) clearTimeout(repair.timer);
      }
      repairs.clear();
    },
  };
}
