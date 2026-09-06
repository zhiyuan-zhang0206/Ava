// Guards the state policy: persistent preferences live in the DB (user_settings);
// localStorage is only for a small, fixed set of EPHEMERAL, per-device selections
// (plus library-managed panel split ratios). This test scans the frontend source
// so a regression — a new preference persisted to localStorage instead of the DB
// — fails here instead of silently shipping an un-synced island.
//
// If you are INTENTIONALLY adding an ephemeral per-device localStorage key,
// update the allowlists below AND annotate the call site with why it is exempt.

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

// Files permitted to call localStorage.setItem directly. Each is an EPHEMERAL,
// per-device selection deliberately kept out of the DB.
const SETITEM_ALLOWLIST = new Set<string>([
  "app/control/config/page.tsx", // control.config.collapsedGroups — per-device group disclosure state
  "components/fleet/fleet-view.tsx", // ava.fleet.mobileTab — current mobile tab
  "lib/use-agents.ts", // ava.active.agent_id — last-viewed agent
]);

// Files permitted to use react-resizable-panels' autoSaveId (a per-viewport
// split ratio persisted by the library through its own sync Storage).
const AUTOSAVE_ALLOWLIST = new Set<string>([
  "components/fleet/fleet-view.tsx", // ava.fleet.split
  "components/fleet/inbox-queue/index.tsx", // ava.fleet.queue-split (dir split, task #1010)
  "components/home-layout.tsx", // home Agent Tree + Inspector panel split ratios (task #2556)
  "app/memory/graph/page.tsx", // ava.memory.graph.split (memory graph side panel, task #2145)
]);

// The complete set of ava-namespaced storage keys allowed to remain in source
// (outside the migration module, which reads + removes the legacy keys).
const ALLOWED_KEYS = new Set<string>([
  "ava.fleet.mobileTab",
  "ava.active.agent_id",
  "ava.fleet.split",
  "ava.fleet.queue-split",
  "ava.memory.graph.split",
  "ava.home.columns.desktop",
  "ava.home.columns.mobile",
  "ava.home.inspector.desktop",
]);

// The one module allowed to reference legacy keys (to read them once + remove).
const MIGRATION_MODULE = "lib/settings-migration.ts";

function sourceFiles(): { rel: string; src: string }[] {
  return readdirSync(SRC_DIR, { recursive: true })
    .map((p) => String(p).split("\\").join("/"))
    .filter((rel) => rel.endsWith(".ts") || rel.endsWith(".tsx"))
    .filter((rel) => !rel.endsWith(".test.ts") && !rel.endsWith(".test.tsx"))
    .filter((rel) => !rel.startsWith("test-support/"))
    .filter((rel) => rel !== MIGRATION_MODULE)
    .map((rel) => ({ rel, src: readFileSync(join(SRC_DIR, rel), "utf8") }));
}

const SOURCE_FILES = sourceFiles();

describe("localStorage state policy", () => {
  it("only allowlisted files call localStorage.setItem", () => {
    const offenders = SOURCE_FILES
      .filter(({ src }) => /localStorage\.setItem\s*\(/.test(src))
      .map(({ rel }) => rel)
      .filter((rel) => !SETITEM_ALLOWLIST.has(rel));
    expect(offenders, "new localStorage.setItem writer — persist to the DB (user_settings) instead").toEqual([]);
  });

  it("only allowlisted files use react-resizable-panels autoSaveId", () => {
    const offenders = SOURCE_FILES
      .filter(({ src }) => /autoSaveId\s*=/.test(src))
      .map(({ rel }) => rel)
      .filter((rel) => !AUTOSAVE_ALLOWLIST.has(rel));
    expect(offenders).toEqual([]);
  });

  it("every ava-namespaced storage key in the allowed files is on the allowlist", () => {
    const files = new Set([...SETITEM_ALLOWLIST, ...AUTOSAVE_ALLOWLIST]);
    const byRel = new Map(SOURCE_FILES.map(({ rel, src }) => [rel, src]));
    const found = new Set<string>();
    for (const f of files) {
      const src = byRel.get(f);
      if (!src) throw new Error(`allowlisted file not found in source scan: ${f}`);
      for (const m of src.matchAll(/["'](ava[.:-][A-Za-z0-9_.:-]*)["']/g)) {
        found.add(m[1]);
      }
    }
    const unexpected = [...found].filter((k) => !ALLOWED_KEYS.has(k));
    expect(unexpected, "unexpected localStorage key — is it an ephemeral selection? otherwise persist to the DB").toEqual([]);
  });
});
