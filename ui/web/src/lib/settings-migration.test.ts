// migrateLegacyLocalStorageSettings — the one-time carry of legacy localStorage
// preference values into user_settings. Covers: non-default → written + key
// dropped; default → dropped without a write; type parsing / validation
// (bool / number / whitelisted window / JSON object); drop-only legacy keys; the
// zustand spawn-prefs blob; exempt keys left untouched; idempotence; and — the
// key safety property — a FAILED write (e.g. 401 before login) keeps the legacy
// key so the value is retried, not lost.

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { migrateLegacyLocalStorageSettings, SETTINGS_MIGRATIONS } from "./settings-migration";
import { USER_SETTING_DEFAULTS } from "./types";

function installLocalStoragePolyfill(): void {
  const store = new Map<string, string>();
  const fake: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
    key: (i) => Array.from(store.keys())[i] ?? null,
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: fake,
    writable: true,
    configurable: true,
  });
}

let write: Mock<(key: string, value: unknown) => Promise<void>>;

beforeEach(() => {
  installLocalStoragePolyfill();
  write = vi.fn<(key: string, value: unknown) => Promise<void>>().mockResolvedValue(undefined);
});

afterEach(() => {
  localStorage.clear();
});

describe("migrateLegacyLocalStorageSettings", () => {
  it("carries a non-default boolean into the DB, then drops the key", async () => {
    // "true" = explicitly opened the inspector (user ruling 2026-08-23: the
    // side panel defaults CLOSED, so an explicit open is a real
    // preference) → written, then the legacy key drops.
    localStorage.setItem("ava:inspector-open", "true");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("display.inspector_open", true);
    expect(localStorage.getItem("ava:inspector-open")).toBeNull();
  });

  it("drops a default-valued key without writing", async () => {
    // "false" = the CLOSED default (side panel, ruling 2026-08-23) → no
    // redundant write, the legacy key just drops.
    localStorage.setItem("ava:inspector-open", "false");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava:inspector-open")).toBeNull();
  });

  it("drops the dead show_reasoning/show_code/show_output keys without writing (setting removed)", async () => {
    localStorage.setItem("ava:show-reasoning", "false");
    localStorage.setItem("ava:show-code", "false");
    localStorage.setItem("ava:show-output", "false");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava:show-reasoning")).toBeNull();
    expect(localStorage.getItem("ava:show-code")).toBeNull();
    expect(localStorage.getItem("ava:show-output")).toBeNull();
  });

  it("drops show_terminated=false because it matches the quiet default", async () => {
    localStorage.setItem("ava.sidebar.showTerminated", "false");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.sidebar.showTerminated")).toBeNull();
  });

  it("migrates show_terminated=true as an explicit history opt-in", async () => {
    localStorage.setItem("ava.sidebar.showTerminated", "true");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("display.show_terminated", true);
    expect(localStorage.getItem("ava.sidebar.showTerminated")).toBeNull();
  });

  it("drops the legacy sidebar width without writing (panel ratios are library-owned)", async () => {
    localStorage.setItem("ava.sidebar.width", "320");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.sidebar.width")).toBeNull();
  });

  it("migrates a valid stats window but drops an out-of-whitelist one", async () => {
    localStorage.setItem("ava.sidebar.statsWindowHours", "72");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("display.stats_window_hours", 72);

    write.mockClear();
    localStorage.setItem("ava.sidebar.statsWindowHours", "999");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.sidebar.statsWindowHours")).toBeNull();
  });

  it("migrates JSON force-params objects (graph + the live .v2 task key)", async () => {
    localStorage.setItem("ava.fleet.forceParams", JSON.stringify({ repulsion: 900 }));
    localStorage.setItem("ava.fleet.taskForceParams.v2", JSON.stringify({ repulsion: 220 }));
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("display.graph_force_params", { repulsion: 900 });
    // #739: the task graph's DB key moved to .v2 too (card → square geometry
    // invalidated pre-refactor tunings), so the legacy localStorage value
    // migrates to the v2 DB key.
    expect(write).toHaveBeenCalledWith("display.task_force_params.v2", { repulsion: 220 });
    expect(localStorage.getItem("ava.fleet.forceParams")).toBeNull();
    expect(localStorage.getItem("ava.fleet.taskForceParams.v2")).toBeNull();
  });

  it("drops malformed JSON without writing", async () => {
    localStorage.setItem("ava.fleet.taskForceParams.v2", "not json");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.fleet.taskForceParams.v2")).toBeNull();
  });

  it("drops the dead pre-v2 task force key without writing", async () => {
    localStorage.setItem("ava.fleet.taskForceParams", JSON.stringify({ repulsion: 999 }));
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.fleet.taskForceParams")).toBeNull();
  });

  it("migrates a valid sidebar sort", async () => {
    localStorage.setItem("ava.sidebar.flatSort.v2", JSON.stringify({ key: "status", dir: "asc" }));
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("display.sidebar_sort", { key: "status", dir: "asc" });
  });

  it("drops the pre-v2 legacy sort key without writing", async () => {
    localStorage.setItem("ava.sidebar.flatSort", JSON.stringify({ key: "last_active", dir: "desc" }));
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.sidebar.flatSort")).toBeNull();
  });

  it("migrates a non-default shell theme; drops the system default", async () => {
    localStorage.setItem("ava:shell-theme", "dark");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("display.shell_terminal_theme", "dark");

    write.mockClear();
    localStorage.setItem("ava:shell-theme", "system");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava:shell-theme")).toBeNull();
  });

  it("never touches EXEMPT keys (mobile tab, active agent, splits)", async () => {
    localStorage.setItem("ava.fleet.mobileTab", "tasks");
    localStorage.setItem("ava.active.agent_id", "7");
    localStorage.setItem("ava.fleet.split", "40,60");
    localStorage.setItem("ava.fleet.queue-split", "42,58");
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava.fleet.mobileTab")).toBe("tasks");
    expect(localStorage.getItem("ava.active.agent_id")).toBe("7");
    expect(localStorage.getItem("ava.fleet.split")).toBe("40,60");
    expect(localStorage.getItem("ava.fleet.queue-split")).toBe("42,58");
  });

  it("migrates the zustand spawn-prefs blob into behavior.spawn_* keys", async () => {
    localStorage.setItem(
      "ava-spawn-prefs",
      JSON.stringify({
        state: { spawnModel: "claude-opus-4-8", spawnPreset: "reviewer", spawnReasoningEffort: "high" },
        version: 0,
      }),
    );
    await migrateLegacyLocalStorageSettings(write);
    expect(write).toHaveBeenCalledWith("behavior.spawn_model", "claude-opus-4-8");
    expect(write).toHaveBeenCalledWith("behavior.spawn_preset", "reviewer");
    expect(write).toHaveBeenCalledWith("behavior.spawn_reasoning_effort", "high");
    expect(localStorage.getItem("ava-spawn-prefs")).toBeNull();
  });

  it("spawn-prefs blob with no set values → dropped without writing", async () => {
    localStorage.setItem("ava-spawn-prefs", JSON.stringify({ state: {}, version: 0 }));
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem("ava-spawn-prefs")).toBeNull();
  });

  it("KEEPS a legacy key when its write fails (e.g. 401 before login)", async () => {
    write.mockRejectedValue(new Error("401"));
    localStorage.setItem("ava:inspector-open", "true"); // non-default (open) → write attempted → fails → kept
    localStorage.setItem("ava-spawn-prefs", JSON.stringify({ state: { spawnModel: "m" }, version: 0 }));
    await migrateLegacyLocalStorageSettings(write);
    // Values are NOT lost — the keys survive for a later (authenticated) retry.
    expect(localStorage.getItem("ava:inspector-open")).toBe("true");
    expect(localStorage.getItem("ava-spawn-prefs")).not.toBeNull();
  });

  it("a failed write does not block dropping default/no-write keys", async () => {
    write.mockRejectedValue(new Error("401"));
    localStorage.setItem("ava:inspector-open", "false"); // default → no write → still dropped
    localStorage.setItem("ava.sidebar.flatSort", "x"); // drop-only → still dropped
    await migrateLegacyLocalStorageSettings(write);
    expect(localStorage.getItem("ava:inspector-open")).toBeNull();
    expect(localStorage.getItem("ava.sidebar.flatSort")).toBeNull();
  });

  it("is idempotent — a second run does nothing", async () => {
    localStorage.setItem("ava:inspector-open", "true"); // non-default → written once
    await migrateLegacyLocalStorageSettings(write);
    write.mockClear();
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
  });

  it("no legacy keys → no writes", async () => {
    await migrateLegacyLocalStorageSettings(write);
    expect(write).not.toHaveBeenCalled();
  });
});

describe("display.inspector_open default (user ruling 2026-08-23: the side panel starts CLOSED on entry)", () => {
  it("USER_SETTING_DEFAULTS has the panel closed", () => {
    expect(USER_SETTING_DEFAULTS["display.inspector_open"]).toBe(false);
  });

  it("the legacy migration treats CLOSED as the default value", () => {
    const entry = SETTINGS_MIGRATIONS.find((m) => m.legacyKey === "ava:inspector-open");
    expect(entry?.default).toBe(false);
  });
});

describe("display.run_timeline_window_hours default", () => {
  it("starts run timelines at the user-approved two-hour window", () => {
    expect(USER_SETTING_DEFAULTS["display.run_timeline_window_hours"]).toBe(2);
  });
});
