// SpawnButton: agent processes only run on agent-runner machines.
// - 0 spawnable agent-runner → button disabled
// - 1 spawnable → plain button, click calls onSpawn({ machine: <name> })
//   (never undefined; the gateway rejects local spawn)
// - >=2 spawnable → Popover picker, alphabetical, click → onSpawn({ machine: name })
// Offline / paused / gateway rows are filtered out of the picker.
//
// Model dropdown: a custom Popover picker with model name left + price right.
// When a non-default model is selected onSpawn receives
// { model: <selected> }; leaving the default yields { model: undefined }.
//
// Reasoning effort select: rendered only for a model that publishes an effort
// ladder, showing the ladder's concrete values with the model's published
// default (reasoning_effort_default — the registry's per-model tuning value)
// pre-selected; no synthetic "Effort: default" option (task #568). A model
// with no ladder — and a model the catalog has not delivered (or no longer
// carries) — renders no select at all, and the spawn request omits
// reasoning_effort. The selection is re-derived from the resolved model, so a
// level the current model does not offer is never sent. A model whose default
// is not concrete keeps the legacy "Effort: default" ("" = provider's own
// default) option — no catalog model does today.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpawnButton } from "./spawn-button";
import type { SystemStatus } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    getSystemStatus: vi.fn(),
    getModels: vi.fn(),
    // Not exercised by any test here (no test renders >0 presets), but real
    // SpawnButton code always fires this query on mount — leaving it absent
    // means every render throws "api.listPresets is not a function" inside
    // the queryFn. React Query swallows that into a silently-failed query
    // (presets stays []), so no test here noticed, but an absent mock is
    // still a landmine — pin it explicit instead of relying on the throw.
    listPresets: vi.fn().mockResolvedValue([]),
  },
}));

// Spawn picker selections are DB-backed (behavior.spawn_*); the reactive mock
// makes them deterministic + re-render on setSetting.
vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { api } from "@/lib/api";
import { resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Default topology in fixtures: current_machine = "cloud" (gateway),
// other entries default to role="agent-runner". Override `role` on an entry
// to test edge cases.
function statusWith(
  machines: {
    name: string;
    online: boolean;
    paused?: boolean | null;
    serveGateway?: boolean;
    serveAgentRunner?: boolean;
  }[],
  current = "cloud",
): SystemStatus {
  return {
    cluster: {
      current_machine: current,
      current_serve_gateway: true,
      current_serve_agent_runner: false,
      current_paused: false,
      machines: machines.map((m) => ({
        name: m.name,
        // Default mirrors the old single-role fixtures: the current machine is
        // gateway-only, every other is an agent-runner. Either flag can be
        // overridden per machine (both can be true for a single box).
        serve_gateway: m.serveGateway ?? m.name === current,
        serve_agent_runner: m.serveAgentRunner ?? m.name !== current,
        identity_mismatch: false,
        settle_waited_on: false,
        is_staging: false,
        gateway_url: `http://${m.name}:8000`,
        up_since_at: "2026-05-19T00:00:00Z",
        online: m.online,
        paused: m.paused !== undefined ? m.paused : m.online ? false : null,
        shell_count: 0,
        agent_count: 0,
        session_count: 0,
        agent_groups: [],
        resource: null,
      })),
    },
    services: { items: [] },
  };
}

const singleMachineStatus = () =>
  statusWith([
    { name: "cloud", online: true },
    { name: "test-host", online: true },
  ]);

const modelsDefault = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {},
  default: "deepseek-v4-pro",
});

const modelsWithSupersession = () => ({
  providers: { deepseek: ["deepseek-v4-pro", "deepseek-v4-flash"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      superseded_by: "deepseek-v4-flash",
    },
    "deepseek-v4-flash": {
      provider: "deepseek",
      context_window: 128_000,
      superseded_by: null,
    },
  },
  default: "deepseek-v4-pro",
});

// Like modelsDefault, but with per-model metadata: deepseek publishes an
// effort ladder, claude publishes none. `null` is what the wire actually
// carries for a model with no ladder — the gateway always emits the key
// (ModelInfo.reasoning_effort_options defaults to None, not omitted).
const modelsWithEffort = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "claude-opus-4-8": {
      provider: "claude",
      context_window: 200_000,
      reasoning_effort_options: null,
      reasoning_effort_default: null,
    },
  },
  default: "deepseek-v4-pro",
});

// Two models with DIFFERENT ladders that overlap on "high" only — the shape
// that makes a carried-over selection observable (real registry ladders do
// diverge: ("high","max") vs ("minimal","low","medium","high")).
const modelsWithTwoLadders = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], openai: ["gpt-5.6"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "gpt-5.6": {
      provider: "openai",
      context_window: 400_000,
      reasoning_effort_options: ["minimal", "low", "medium", "high"],
      reasoning_effort_default: "medium",
    },
  },
  default: "deepseek-v4-pro",
});

// A ladder WITHOUT a concrete default — the legacy shape (no catalog model
// today; kept for the fallback path where "Effort: default" must stay
// expressible). `null` is what the wire sends for a model with no default.
const modelsWithLegacyDefault = () => ({
  providers: { deepseek: ["deepseek-v4-pro"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: null,
    },
  },
  default: "deepseek-v4-pro",
});

// Ultra Speed Worker-style preset: carries llm_model (+ optionally
// reasoning_effort), as agent_presets.config does on the wire.
const presetRow = (
  overrides: Partial<{
    name: string;
    label: string;
    config: Record<string, unknown>;
  }> = {},
) => ({
  id: 6,
  name: "ultra-speed-worker",
  label: "Ultra Speed Worker",
  description: null,
  config: { llm_model: "mimo-v2.5-pro-ultraspeed" },
  created_at: "2026-07-24T00:09:50.002371-07:00",
  updated_at: "2026-07-24T00:09:50.002371-07:00",
  ...overrides,
});

// deepseek + mimo — the shape a preset-model override test needs (the preset
// carries mimo-v2.5-pro-ultraspeed, the cluster default is deepseek).
const modelsWithMimo = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], mimo: ["mimo-v2.5-pro-ultraspeed"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "mimo-v2.5-pro-ultraspeed": {
      provider: "mimo",
      context_window: 1_000_000,
      reasoning_effort_options: ["none", "high"],
      reasoning_effort_default: "high",
    },
  },
  default: "deepseek-v4-pro",
});

// Helper: open the model picker popover and click a model option by its text.
// Scoped to the popover's list — the trigger button shows the current model
// name too, so an unscoped getByText can match twice.
async function selectModel(modelName: string) {
  fireEvent.click(screen.getByLabelText("Model"));
  // findByRole waits for the popover's list to actually be in the DOM (the
  // click → open → portal-mount sequence is async; under the parallel full
  // suite the waitFor(getByText("Models")) above raced the list mount).
  // Radix Popover is controlled (open={modelOpen}); clicking the trigger
  // while the preset popover's dismissal is still settling can leave the
  // fresh popover closed (observed as a ~25% flake in the full suite: the
  // popover either never opens or opens and is immediately dismissed, so
  // findByRole("list") below times out). Retry the toggle until the list is
  // actually in the DOM — each click flips the controlled state, so a
  // dismissed-open ends with a plain closed→open on the next attempt.
  const trigger = screen.getByLabelText("Model");
  let list: HTMLElement;
  for (let attempt = 0; ; attempt++) {
    fireEvent.click(trigger);
    try {
      list = await screen.findByRole("list", undefined, { timeout: 500 });
      break;
    } catch {
      if (attempt >= 2) throw new Error(
        "model popover did not open after 3 toggle attempts",
      );
    }
  }
  fireEvent.click(within(list).getByText(modelName));
  // The selection is DB-backed user settings — the write + re-render is
  // async, so wait for the trigger to reflect the new model before returning
  // (a bare click could leave a caller's next assertion racing the update;
  // observed as a flake in "explicit model pick after a preset still wins").
  await waitFor(() => {
    expect(screen.getByLabelText("Model").textContent).toContain(modelName);
  });
  // The selection is DB-backed user settings — the write + re-render is
  // async, so wait for the trigger to reflect the new model before returning
  // (a bare click could leave a caller's next assertion racing the update;
  // observed as a flake in "explicit model pick after a preset still wins").
  await waitFor(() => {
    expect(screen.getByLabelText("Model").textContent).toContain(modelName);
  });
}

// Wait until React renders the multi-machine variant (Popover sets
// aria-haspopup=dialog on the trigger).
async function waitForPopoverRender() {
  await waitFor(() => {
    expect(
      screen.getByLabelText("Spawn agent").getAttribute("aria-haspopup"),
    ).toBe("dialog");
  });
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  // Selections are DB-backed user settings — reset the mock so tests are isolated.
  resetMockSettings();
  // Default: models query returns empty (most tests don't need model picker).
  vi.mocked(api.getModels).mockResolvedValue({
    providers: {},
    models: {},
    default: "",
  });
});

describe("SpawnButton status polling", () => {
  it("does not refetch before the shared 15 second cadence", async () => {
    vi.useFakeTimers();
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    const view = wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    try {
      await act(async () => {
        await Promise.resolve();
      });
      expect(api.getSystemStatus).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(14_999);
      });
      expect(api.getSystemStatus).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(api.getSystemStatus).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});

describe("SpawnButton zero-spawnable", () => {
  it("status not loaded yet → renders disabled button (no machines visible)", () => {
    vi.mocked(api.getSystemStatus).mockReturnValue(new Promise(() => undefined));
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);
    const btn = screen.getByLabelText("Spawn agent");
    expect(btn.hasAttribute("disabled")).toBe(true);
    fireEvent.click(btn);
    expect(onSpawn).not.toHaveBeenCalled();
  });

  it("only gateway online → disabled (no agent-runner)", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([{ name: "cloud", online: true }]),
    );
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);
    await waitFor(() => {
      expect(api.getSystemStatus).toHaveBeenCalled();
    });
    const btn = screen.getByLabelText("Spawn agent");
    expect(btn.hasAttribute("disabled")).toBe(true);
    fireEvent.click(btn);
    expect(onSpawn).not.toHaveBeenCalled();
  });

  it("agent-runner all offline → disabled", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: false },
        { name: "test-host-2", online: false },
      ]),
    );
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitFor(() => {
      expect(api.getSystemStatus).toHaveBeenCalled();
    });
    expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(true);
  });

  it("agent-runner all paused → disabled", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true, paused: true },
      ]),
    );
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitFor(() => {
      expect(api.getSystemStatus).toHaveBeenCalled();
    });
    expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(true);
  });
});

describe("SpawnButton single-spawnable", () => {
  it("only one agent-runner online → plain button, click calls onSpawn({ machine: name })", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
      ]),
    );
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(
        false,
      );
    });
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({ machine: "test-host", model: undefined });
    expect(screen.queryByText("Spawn on")).toBeNull();
  });

  it("one agent-runner online + one paused → falls back to single-spawnable", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true, paused: false },
        { name: "test-host-2", online: true, paused: true },
      ]),
    );
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(
        false,
      );
    });
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({ machine: "test-host", model: undefined });
  });
});

describe("SpawnButton multi-spawnable picker", () => {
  it("two agent-runner online → popover with both, alphabetical", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
        { name: "test-host-2", online: true },
      ]),
    );
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitForPopoverRender();
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    await waitFor(() => {
      expect(screen.getByText("Spawn on")).toBeTruthy();
    });
    expect(screen.getByText("test-host-2")).toBeTruthy();
    expect(screen.getByText("test-host")).toBeTruthy();
    // cloud is gateway → not in picker
    expect(screen.queryByText("cloud")).toBeNull();
  });

  it("click an entry → onSpawn({ machine: name, model: undefined })", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
        { name: "test-host-2", online: true },
      ]),
    );
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);
    await waitForPopoverRender();
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    await waitFor(() => {
      expect(screen.getByText("Spawn on")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("test-host"));
    expect(onSpawn).toHaveBeenCalledWith({ machine: "test-host", model: undefined });
  });

  it("offline agent-runner not rendered in picker", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
        { name: "test-host-2", online: true },
        { name: "dead", online: false },
      ]),
    );
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitForPopoverRender();
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    await waitFor(() => {
      expect(screen.getByText("Spawn on")).toBeTruthy();
    });
    expect(screen.queryByText("dead")).toBeNull();
  });

  it("paused agent-runner not rendered in picker", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true, paused: false },
        { name: "test-host-2", online: true, paused: false },
        { name: "frozen", online: true, paused: true },
      ]),
    );
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitForPopoverRender();
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    await waitFor(() => {
      expect(screen.getByText("Spawn on")).toBeTruthy();
    });
    expect(screen.queryByText("frozen")).toBeNull();
  });
});

describe("SpawnButton right-alignment", () => {
  // Regression guard for #758's spacer-based right-align (a
  // `flex-1 min-w-0` spacer pushes the trigger to the far right of the
  // flex-nowrap row) — beb45f43 later dropped the spacer from the
  // ≥2-machine popover branch while keeping it in the 1-machine branch.
  // jsdom does no real flex layout, so this asserts DOM structure (the
  // spacer immediately precedes the trigger) rather than pixel position.
  function spacerBeforeTrigger(container: HTMLElement): boolean {
    const btn = within(container).getByLabelText("Spawn agent");
    const row = btn.closest(".flex-nowrap");
    if (!row) throw new Error("spawn row container not found");
    const children = Array.from(row.children);
    const btnIndex = children.indexOf(btn);
    if (btnIndex < 1) return false;
    const prev = children[btnIndex - 1];
    return prev.getAttribute("aria-hidden") === "true" && prev.className.includes("flex-1");
  }

  it("1 spawnable (plain button) → spacer right-aligns the trigger", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    const { container } = wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });
    expect(spacerBeforeTrigger(container)).toBe(true);
  });

  it("≥2 spawnable (popover trigger) → spacer right-aligns the trigger", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
        { name: "test-host-2", online: true },
      ]),
    );
    const { container } = wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitForPopoverRender();
    expect(spacerBeforeTrigger(container)).toBe(true);
  });
});

describe("SpawnButton variant", () => {
  it("variant='icon' → renders icon-only button (collapsed sidebar)", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
      ]),
    );
    wrap(<SpawnButton variant="icon" onSpawn={vi.fn()} />);
    await waitFor(() => {
      expect(api.getSystemStatus).toHaveBeenCalled();
    });
    expect(screen.getByLabelText("Spawn agent")).toBeTruthy();
    expect(screen.queryByText("Spawn")).toBeNull();
  });
});

describe("SpawnButton model dropdown", () => {
  it("omits a registry-superseded model while keeping its replacement", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithSupersession());
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    const trigger = await screen.findByLabelText("Model");
    fireEvent.click(trigger);
    const list = await screen.findByRole("list");

    expect(within(list).queryByText("deepseek-v4-pro")).toBeNull();
    expect(within(list).getByText("deepseek-v4-flash")).toBeTruthy();
  });

  it("selecting a non-default model passes model to onSpawn", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    // Wait for both queries to resolve
    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // Open model picker popover and select a non-default model
    await selectModel("claude-opus-4-8");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "claude-opus-4-8",
    });
  });

  it("keeps the selected model across a remount (spawn many on one model)", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());

    // First mount: pick a non-default model + spawn.
    const first = wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Model")).toBeTruthy());
    await selectModel("claude-opus-4-8");
    // Unmount (simulates the remount that previously reset the selection).
    first.unmount();

    // Remount: the picker trigger still shows the previously chosen model, and
    // a spawn carries it — no re-picking needed.
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);
    await waitFor(() => expect(screen.getByLabelText("Model")).toBeTruthy());
    // The button text shows the selected model name
    expect(screen.getByLabelText("Model").textContent).toContain("claude-opus-4-8");
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "claude-opus-4-8",
    });
  });

  it("leaving the default model passes model: undefined to onSpawn", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // Do not change the picker — stays on the default
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: undefined,
    });
  });

  it("explicitly selecting the default model sends it (beats a preset's pinned model)", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // Change to non-default, then back to default — the explicit pick is
    // stored and must be sent, or a preset's pinned llm_model would silently
    // win over what the user visibly selected (task #568).
    await selectModel("claude-opus-4-8");
    await selectModel("deepseek-v4-pro");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "deepseek-v4-pro",
    });
  });

  it("no models returned → no model picker rendered", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue({ providers: {}, models: {}, default: "" });
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    await waitFor(() => {
      expect(api.getModels).toHaveBeenCalled();
    });
    expect(screen.queryByLabelText("Model")).toBeNull();
  });

  it("default model (no explicit selection) → effort select appears for the default", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    // With the resolved model (default), the effort select now appears
    // without needing a manual model switch.
    await waitFor(() => {
      expect(screen.getByLabelText("Reasoning effort")).toBeTruthy();
    });
  });

  it("model with no effort ladder → no effort select at all; spawn omits reasoning_effort", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    await selectModel("claude-opus-4-8");
    // A select whose only entry is "Effort: default" controls nothing — the
    // whole control is gone rather than rendered inert.
    expect(screen.queryByLabelText("Reasoning effort")).toBeNull();

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "claude-opus-4-8",
      reasoning_effort: undefined,
    });
  });

  // The models query has not resolved, but the stored spawn_model already
  // has (user settings and /api/models are independent queries) — so a
  // resolved model name with no catalog entry behind it is the state of every
  // page load for a user who has ever picked a model. Nothing is known about
  // its ladder yet, so no effort control is offered.
  it("models still loading with a stored model → no effort select yet", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockReturnValue(new Promise(() => undefined));
    setMockSetting("behavior.spawn_model", "deepseek-v4-pro");
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });
    expect(screen.queryByLabelText("Reasoning effort")).toBeNull();
  });

  // A stored spawn_model naming a model the catalog no longer carries (model
  // ids do get renamed / retired) resolves to no entry — same unknown-ladder
  // state as the loading window, same answer.
  it("stored model absent from the catalog → no effort select", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    setMockSetting("behavior.spawn_model", "deepseek-v3-retired");
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    expect(screen.queryByLabelText("Reasoning effort")).toBeNull();
  });

  it("model with effort options → selecting one passes reasoning_effort to onSpawn", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // deepseek-v4-pro is the cluster default — the effort select appears
    // immediately (no manual switch needed), pre-selected at the model's
    // published default ("max"). No synthetic "Effort: default" option.
    await selectModel("deepseek-v4-pro");
    const effortSelect = screen.getByLabelText<HTMLSelectElement>("Reasoning effort");
    expect([...effortSelect.options].map((o) => o.text)).toEqual(["high", "max"]);
    expect(effortSelect.value).toBe("max");

    // An explicit pick replaces the pre-selected default.
    fireEvent.change(effortSelect, { target: { value: "high" } });

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "deepseek-v4-pro",
      reasoning_effort: "high",
    });
  });

  it("switching to a model without a ladder drops a leftover effort selection", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    await selectModel("deepseek-v4-pro");
    fireEvent.change(screen.getByLabelText("Reasoning effort"), {
      target: { value: "max" },
    });
    await selectModel("claude-opus-4-8");

    expect(screen.queryByLabelText("Reasoning effort")).toBeNull();
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "claude-opus-4-8",
      reasoning_effort: undefined,
    });
  });

  // The silent-divergence case: both models have a ladder, so the select stays
  // rendered — but "max" is not on gpt-5.6's. Without re-deriving, the select
  // shows no selected option (nothing matches) while the spawn request still
  // carries "max": what the user sees and what is sent come apart. With the
  // published default, the drop lands on the new model's own default instead
  // of an unselected blank.
  it("switching ladders drops a selection the new model does not offer", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithTwoLadders());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    await selectModel("deepseek-v4-pro");
    fireEvent.change(screen.getByLabelText("Reasoning effort"), {
      target: { value: "max" },
    });
    await selectModel("gpt-5.6");

    const effortSelect = screen.getByLabelText<HTMLSelectElement>("Reasoning effort");
    expect([...effortSelect.options].map((o) => o.text)).toEqual([
      "minimal",
      "low",
      "medium",
      "high",
    ]);
    // The stale "max" is dropped, and the new model's own default takes over.
    expect(effortSelect.value).toBe("medium");
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "gpt-5.6",
      reasoning_effort: "medium",
    });
  });

  it("a level both ladders offer survives the model switch", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithTwoLadders());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    await selectModel("deepseek-v4-pro");
    fireEvent.change(screen.getByLabelText("Reasoning effort"), {
      target: { value: "high" },
    });
    await selectModel("gpt-5.6");

    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("high");
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "gpt-5.6",
      reasoning_effort: "high",
    });
  });

  // Task #568: no synthetic "Effort: default" option — the model's own default
  // is pre-selected the moment the model resolves, and a spawn with no manual
  // effort pick carries it (what you see is what is sent).
  it("no explicit effort → model default is pre-selected and sent", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // deepseek-v4-pro is the default model; its default effort "max" is
    // pre-selected with no interaction at all.
    const effortSelect = screen.getByLabelText<HTMLSelectElement>("Reasoning effort");
    expect(effortSelect.value).toBe("max");
    expect(screen.queryByText("Effort: default")).toBeNull();

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: undefined,
      reasoning_effort: "max",
    });
  });

  // Switching models follows the new model's default — the stored "" (never
  // touched) resolves to whichever model is selected.
  it("switching models re-derives the pre-selected default", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithTwoLadders());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("max");
    await selectModel("gpt-5.6");
    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("medium");
    // And back: deepseek's default returns.
    await selectModel("deepseek-v4-pro");
    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("max");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "deepseek-v4-pro",
      reasoning_effort: "max",
    });
  });

  // No catalog model publishes a ladder without a concrete default today, but
  // the fallback must stay expressible: the legacy "Effort: default" option
  // ("" — sends nothing, provider's own default applies).
  it("ladder without a concrete default keeps the legacy Effort: default option", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithLegacyDefault());
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    const effortSelect = screen.getByLabelText<HTMLSelectElement>("Reasoning effort");
    expect([...effortSelect.options].map((o) => o.text)).toEqual([
      "Effort: default",
      "high",
      "max",
    ]);
    expect(effortSelect.value).toBe("");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: undefined,
      reasoning_effort: undefined,
    });
  });

  // Task #568: picking a preset that carries a model (config.llm_model) must
  // override the previously selected model — the user's exact complaint ("选中
  // Ultra Speed Worker，model 我以前选中了什么就还是什么").
  it("preset with a model overrides the model picker and the spawn", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithMimo());
    vi.mocked(api.listPresets).mockResolvedValue([presetRow()]);
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Preset")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // Start on the cluster default (deepseek) — the stored spawn_model is unset.
    expect(screen.getByLabelText("Model").textContent).toContain("deepseek-v4-pro");
    // #723r2: the preset picker is a Popover button now (fixed width +
    // ellipsis — a native select cannot render an ellipsis).
    fireEvent.click(screen.getByLabelText("Preset"));
    fireEvent.click(screen.getByText("Ultra Speed Worker"));

    // Model picker follows the preset's model, effort follows that model's default.
    expect(screen.getByLabelText("Model").textContent).toContain("mimo-v2.5-pro-ultraspeed");
    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("high");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "mimo-v2.5-pro-ultraspeed",
      preset: "ultra-speed-worker",
      reasoning_effort: "high",
    });
  });

  it("preset without a model leaves the model selection alone", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithMimo());
    vi.mocked(api.listPresets).mockResolvedValue([
      presetRow({ config: { skills_to_inject_into_system_prompt: ["gmail"] } }),
    ]);
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Preset")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // #723r2: the preset picker is a Popover button now (fixed width +
    // ellipsis — a native select cannot render an ellipsis).
    fireEvent.click(screen.getByLabelText("Preset"));
    fireEvent.click(screen.getByText("Ultra Speed Worker"));

    expect(screen.getByLabelText("Model").textContent).toContain("deepseek-v4-pro");
    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("max");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: undefined,
      preset: "ultra-speed-worker",
      reasoning_effort: "max",
    });
  });

  // A preset that pins reasoning_effort carries it into the picker (and the
  // spawn), even when it does not carry a model.
  it("preset with reasoning_effort overrides the effort picker", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithMimo());
    vi.mocked(api.listPresets).mockResolvedValue([
      presetRow({ config: { reasoning_effort: "high" } }),
    ]);
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Preset")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // #723r2: the preset picker is a Popover button now (fixed width +
    // ellipsis — a native select cannot render an ellipsis).
    fireEvent.click(screen.getByLabelText("Preset"));
    fireEvent.click(screen.getByText("Ultra Speed Worker"));

    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("high");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: undefined,
      preset: "ultra-speed-worker",
      reasoning_effort: "high",
    });
  });

  // The preset's model is a seed, not a lock: an explicit pick after the preset
  // still wins (backend merge: explicit config beats preset config per key).
  it("explicit model pick after a preset still wins", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithMimo());
    vi.mocked(api.listPresets).mockResolvedValue([presetRow()]);
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Preset")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });

    // #723r2: the preset picker is a Popover button now (fixed width +
    // ellipsis — a native select cannot render an ellipsis).
    fireEvent.click(screen.getByLabelText("Preset"));
    fireEvent.click(screen.getByText("Ultra Speed Worker"));
    expect(screen.getByLabelText("Model").textContent).toContain("mimo-v2.5-pro-ultraspeed");

    // Explicitly switch back to deepseek — the preset stays selected (it still
    // seeds skills etc.), but the explicit model wins on spawn.
    await selectModel("deepseek-v4-pro");
    expect(screen.getByLabelText<HTMLSelectElement>("Reasoning effort").value).toBe("max");

    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "deepseek-v4-pro",
      preset: "ultra-speed-worker",
      reasoning_effort: "max",
    });
  });

  it("models loading → spawn still works (no model picker yet)", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    // Never resolves — simulates loading state
    vi.mocked(api.getModels).mockReturnValue(new Promise(() => undefined));
    const onSpawn = vi.fn();
    wrap(<SpawnButton variant="sm" onSpawn={onSpawn} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Spawn agent").hasAttribute("disabled")).toBe(false);
    });
    expect(screen.queryByLabelText("Model")).toBeNull();
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: undefined,
    });
  });

  it("groups the model list by provider with a header per group", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    fireEvent.click(screen.getByLabelText("Model"));

    const list = await screen.findByRole("list");
    // Single flat <ul> (one "list" role) with a provider header row ahead
    // of that provider's models — not a nested list per provider.
    expect(within(list).getByText("DeepSeek")).toBeTruthy();
    expect(within(list).getByText("Claude")).toBeTruthy();
    expect(within(list).getByText("deepseek-v4-pro")).toBeTruthy();
    expect(within(list).getByText("claude-opus-4-8")).toBeTruthy();
  });

  it("roster-hidden models are counted and excluded, with empty providers dropped", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    const roster = modelsDefault();
    roster.providers.deepseek.push("deepseek-v4-flash");
    vi.mocked(api.getModels).mockResolvedValue(roster);
    setMockSetting("models.hidden", ["claude-opus-4-8", "deepseek-v4-flash"]);
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Model")).toBeTruthy();
    });
    fireEvent.click(screen.getByLabelText("Model"));

    const list = await screen.findByRole("list");
    expect(within(list).getByText("deepseek-v4-pro")).toBeTruthy();
    expect(within(list).queryByText("deepseek-v4-flash")).toBeNull();
    expect(within(list).queryByText("claude-opus-4-8")).toBeNull();
    expect(within(list).queryByText("Claude")).toBeNull();
    expect(screen.getByText("2 hidden — manage in Control > Display")).toBeTruthy();
  });

  it("no hidden models → no hidden-model footer", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    fireEvent.click(await screen.findByLabelText("Model"));
    await screen.findByRole("list");

    expect(screen.queryByText(/hidden — manage in Control > Display/)).toBeNull();
  });

  it("stale hidden model ids outside the roster do not produce a footer", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    setMockSetting("models.hidden", ["claude-opus-4-7-retired"]);
    wrap(<SpawnButton variant="sm" onSpawn={vi.fn()} />);

    fireEvent.click(await screen.findByLabelText("Model"));
    await screen.findByRole("list");

    expect(screen.queryByText(/hidden — manage in Control > Display/)).toBeNull();
  });
});
