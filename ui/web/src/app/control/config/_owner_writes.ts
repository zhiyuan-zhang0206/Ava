/** Each owner persists independently; keep every verdict and successful restart. */
import type { ConfigFieldView, ConfigWriteResult } from "@/lib/contracts/types";

export async function writeConfigOwners(
  body: Record<string, unknown>,
  fields: readonly Pick<ConfigFieldView, "name" | "owner">[],
  write: (patch: Record<string, unknown>) => Promise<ConfigWriteResult>,
): Promise<ConfigWriteResult> {
  const owners = new Map(
    fields.map((field) => [field.name, field.owner ?? null]),
  );
  const groups = new Map<string | null, Record<string, unknown>>();
  for (const [name, value] of Object.entries(body)) {
    if (!owners.has(name)) throw new Error(`unknown config field: ${name}`);
    const owner = owners.get(name) ?? null;
    const group = groups.get(owner) ?? {};
    group[name] = value;
    groups.set(owner, group);
  }
  const results: ConfigWriteResult["results"] = {};
  const restart = new Set<string>();
  let applied = true;
  for (const patch of groups.values()) {
    try {
      const result = await write(patch);
      Object.assign(results, result.results);
      for (const target of result.restart_required) restart.add(target);
      applied &&= result.applied;
    } catch (error) {
      applied = false;
      const reason = error instanceof Error ? error.message : String(error);
      for (const name of Object.keys(patch))
        results[name] = { ok: false, reason };
    }
  }
  return { applied, results, restart_required: [...restart].sort() };
}
