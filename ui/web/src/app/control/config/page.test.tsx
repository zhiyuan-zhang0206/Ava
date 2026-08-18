// /control/config page tests — loading/error/no-data tri-state + bool
// toggle + text inline edit (Enter commit / Escape cancel / no-op cancel on
// unchanged value) + restart hint + the per-machine selector (cluster vs
// agent-runner view, host remote_writable editability, capability pre-grey,
// write-result per-field rejection + value revert, restart banner from the
// PUT result) + the tag-filter bar.
//
// Page groups fields into the 17 display groups of _config_groups.ts (a
// frontend regrouping — env var → group, falling back by the backend's domain
// group, then to "Other"). Each row shows a label derived from the env var —
// AVA_ prefix stripped, underscores to spaces, case preserved (the env var
// rides in the title attribute) plus tag badges:
// Runtime / CLI-only, the raw scope, per-agent, "Startup: <target>". A remote
// agent-runner view drops fields of capabilities the machine doesn't carry and
// shows a "(edit on Cluster view)" hint for non-host fields. Sensitive fields
// are always read-only (masked + lock + CLI-only).
//
// happy-dom + RTL + real QueryClient (mock at the
// api.{getConfig,putConfig,getMachines,getSystemStatus} layer so useQuery /
// useMutation goes through its full lifecycle).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type {
  AgentMachineRow,
  ConfigView,
  ConfigWriteResult,
  ModelsResponse,
  ResolvedConfigView,
  SystemStatus,
} from "@/lib/types";

import ConfigPage from "./page";

afterEach(cleanup);

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(ui: React.ReactElement, qc?: QueryClient) {
  const client = qc ?? makeQc();
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

// The page renders TWO independently async panels that share one label/badge
// vocabulary: the editable group list (a single getConfig read) and the
// read-only per-model list above it (getModels, then getResolvedConfig gated on
// the model it returns — a two-hop chain that normally lands later). So a
// document-wide text query is only stable once BOTH have rendered: awaiting a
// group-list element leaves the per-model rows in flight, and whether they
// arrive before the assertion is scheduler timing. That is the `expected 4 to
// be 3` that failed CI on PR #908 under a loaded box — 4 is the settled truth
// (three group rows plus one per-model row), 3 was a half-rendered page.
//
// Render through this helper so assertions always see the final DOM, and count
// group-list badges with `inGroups` so a per-model row can never be swept in.
async function renderSettled() {
  const rendered = wrap(<ConfigPage />);
  await screen.findByText("LOG LEVEL");
  // A per-model row exists only once both hops have resolved — the last thing
  // on the page to render.
  await screen.findByTestId("per-model-source-auto_compact_fraction");
  return rendered;
}

/** Matches of `text` inside the editable group list, never the per-model panel. */
function inGroups(text: string): HTMLElement[] {
  return screen
    .getAllByTestId(/^config-group-/)
    .flatMap((group) => within(group).queryAllByText(text));
}

const GATEWAY = "cp-host";
const AGENT_RUNNER = "runner-1";

const MACHINES: AgentMachineRow[] = [
  { name: GATEWAY, description: "gateway", live: true, is_staging: false },
  { name: AGENT_RUNNER, description: "agent-runner", live: true, is_staging: false },
  { name: "offline-host", description: "down", live: false, is_staging: false },
];

const STATUS = {
  cluster: { current_machine: GATEWAY },
} as unknown as SystemStatus;

const OK_RESULT = (
  names: string[],
  restart: string[] = [],
): ConfigWriteResult => ({
  applied: true,
  results: Object.fromEntries(names.map((n) => [n, { ok: true, reason: null }])),
  restart_required: restart,
});

// Fixture spans all three capabilities so the remote-hides-gateway-fields
// behavior is exercised. Display groups (via _config_groups.ts): AVA_LOG_LEVEL
// + AVA_MAX_RETRIES → "LLM settings" (backend-group fallback), AVA_ENABLE_COMPACT
// + AVA_API_KEY + AVA_NUDGE_CADENCE → "Agent execution" (Agent fallback),
// AVA_BUILD_SHA + AVA_HOST_LABEL → "Display & general" (General fallback),
// AVA_BROWSER_ENABLED → "Services" (static map), AVA_GATEWAY_PORT →
// "Connection" (static map).
const VIEW: ConfigView = {
  fields: [
    {
      name: "log_level",
      field_type: "string",
      current_value: "INFO",
      default_value: "INFO",
      description: "Logging verbosity",
      group: "LLM",
      capability: "agent-runner",
      scope: "cluster-default",
      restart_required: "all",
      writable: true,
      sensitive: false,
      env_var: "AVA_LOG_LEVEL",
      remote_writable: false,
      per_agent: true,
      can_enable: null,
      reason: null,
    },
    {
      name: "max_retries",
      field_type: "int",
      current_value: 3,
      default_value: 3,
      description: "Retry count",
      group: "LLM",
      capability: "agent-runner",
      scope: "cluster-default",
      restart_required: "",
      writable: true,
      sensitive: false,
      env_var: "AVA_MAX_RETRIES",
      remote_writable: false,
      per_agent: true,
      can_enable: null,
      reason: null,
    },
    {
      name: "enable_compact",
      field_type: "bool",
      current_value: true,
      default_value: false,
      description: "",
      group: "Agent",
      capability: "agent-runner",
      scope: "agent",
      restart_required: "",
      writable: true,
      sensitive: false,
      env_var: "AVA_ENABLE_COMPACT",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "api_key",
      field_type: "string",
      current_value: "secret-xyz",
      default_value: "",
      description: "API key (sensitive)",
      group: "Agent",
      capability: "agent-runner",
      scope: "cluster-pinned",
      restart_required: "",
      writable: true,
      sensitive: true,
      env_var: "AVA_API_KEY",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "build_sha",
      field_type: "string",
      current_value: "abc123",
      default_value: null,
      description: "Build SHA",
      group: "General",
      capability: "common",
      scope: "host",
      restart_required: "",
      writable: false,
      sensitive: false,
      env_var: "AVA_BUILD_SHA",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "host_label",
      field_type: "string",
      current_value: "rack-A",
      default_value: "",
      description: "Free-text label for the host",
      group: "General",
      capability: "common",
      scope: "host",
      restart_required: "gateway",
      writable: true,
      sensitive: false,
      env_var: "AVA_HOST_LABEL",
      remote_writable: true,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "browser_enabled",
      field_type: "bool",
      current_value: false,
      default_value: false,
      description: "Headed browser",
      group: "Services",
      capability: "agent-runner",
      scope: "host",
      restart_required: "",
      writable: true,
      sensitive: false,
      env_var: "AVA_BROWSER_ENABLED",
      remote_writable: true,
      per_agent: false,
      can_enable: false,
      reason: "no display detected",
    },
    {
      name: "nudge_cadence",
      field_type: "enum",
      current_value: "once_per_compaction",
      default_value: "once_per_compaction",
      description: "How often the nudge fires",
      group: "Agent",
      capability: "agent-runner",
      scope: "cluster-default",
      restart_required: "agent",
      writable: true,
      sensitive: false,
      env_var: "AVA_NUDGE_CADENCE",
      remote_writable: false,
      per_agent: true,
      choices: ["once_per_compaction", "every_time"],
      can_enable: null,
      reason: null,
    },
    {
      name: "gateway_port",
      field_type: "int",
      current_value: 8000,
      default_value: 8000,
      description: "Gateway bind port",
      group: "Data plane",
      capability: "gateway",
      scope: "host",
      restart_required: "gateway",
      writable: false,
      sensitive: false,
      env_var: "AVA_GATEWAY_PORT",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
  ],
  raw_overrides: { log_level: "INFO" },
  // The default fixture is a pure agent-runner: a remote view of it drops the
  // gateway section. Tests that need a co-located box override this.
  machine_capabilities: ["agent-runner"],
};

// The per-model resolution view (read-only, above the editable groups) is part
// of the page, so its two reads are mocked for every test here. `max_retries`
// appears in BOTH fixtures on purpose: it is the row the "go to editor" link
// jumps to, and a resolved row is a real config field by contract.
const MODELS: ModelsResponse = {
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-5"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 1_000_000,
      pricing: null,
      reasoning_effort_options: null,
    },
    "claude-opus-5": {
      provider: "claude",
      context_window: 1_000_000,
      pricing: null,
      reasoning_effort_options: null,
    },
  },
  default: "deepseek-v4-pro",
};

const RESOLVED: ResolvedConfigView = {
  model: "deepseek-v4-pro",
  registered: true,
  fields: [
    {
      name: "auto_compact_fraction",
      env_var: "AVA_AUTO_COMPACT_FRACTION",
      description: "Force-compact ceiling",
      field_type: "float",
      choices: null,
      group: "Agent",
      effective_value: 0.55,
      source: "model-default",
      explicit_value: null,
      model_default: 0.55,
      shared_default: 0.8,
      per_agent: true,
      restart_required: "agent",
    },
    {
      name: "max_retries",
      env_var: "AVA_MAX_RETRIES",
      description: "Retry count",
      field_type: "int",
      choices: null,
      group: "LLM",
      effective_value: 9,
      source: "explicit",
      explicit_value: 9,
      model_default: 4,
      shared_default: 6,
      per_agent: false,
      restart_required: "agent",
    },
    {
      name: "llm_retry_max_attempts",
      env_var: "AVA_LLM_RETRY_MAX_ATTEMPTS",
      description: "Retry attempts",
      field_type: "int",
      choices: null,
      group: "LLM",
      effective_value: 6,
      source: "shared-default",
      explicit_value: null,
      model_default: null,
      shared_default: 6,
      per_agent: false,
      restart_required: "agent",
    },
  ],
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getMachines").mockResolvedValue(MACHINES);
  vi.spyOn(api, "getSystemStatus").mockResolvedValue(STATUS);
  vi.spyOn(api, "getModels").mockResolvedValue(MODELS);
  vi.spyOn(api, "getResolvedConfig").mockResolvedValue(RESOLVED);
  // Third async panel: the cluster default-model control (its own tests live in
  // _default_model.test.tsx; here it only has to not reach the network).
  vi.spyOn(api, "getDefaultModel").mockResolvedValue({
    model: "deepseek-v4-pro",
    source: "cluster",
  });
});

describe("ConfigPage tri-state", () => {
  it("loading shows loading text", () => {
    vi.spyOn(api, "getConfig").mockReturnValue(
      new Promise(() => undefined),
    );
    wrap(<ConfigPage />);
    expect(screen.getByText(/Loading config/)).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    vi.spyOn(api, "getConfig").mockRejectedValue(new Error("network down"));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByText(/Couldn't load config/));
    expect(screen.queryByText(/network down/)).toBeNull();
  });
});

describe("ConfigPage render", () => {
  it("display-group headers render with raw case-preserved field labels", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    // Display-group headers from _config_groups.ts (backend-group fallback +
    // static-map hits).
    expect(screen.getByText("LLM settings")).toBeTruthy();
    expect(screen.getByText("Agent execution")).toBeTruthy();
    expect(screen.getByText("Display & general")).toBeTruthy();
    expect(screen.getByText("Connection")).toBeTruthy();
    // Case-preserved labels (AVA_ stripped, underscores to spaces); the env var
    // rides in the title attribute, and the python field name is not rendered.
    expect(screen.getByTitle("AVA_LOG_LEVEL")).toBeTruthy();
    // Scoped: max_retries is also a per-model row, so a document-wide getByText
    // would find two on a settled page.
    expect(inGroups("MAX RETRIES")).toHaveLength(1);
    expect(screen.queryByText("log_level")).toBeNull();
    expect(screen.queryByText("AVA_LOG_LEVEL")).toBeNull();
  });

  it("HIDDEN_ENV_VARS: eval-harness plumbing is dropped from the panel", async () => {
    // AVA_CONTAINER_EXEC / AVA_OUTPUT_DIR are eval-driver-set, and their env vars
    // are in HIDDEN_ENV_VARS — they must not render even though the backend serves
    // them in the field list (matching keys on env_var, not the python name).
    const viewWithEvalField: ConfigView = {
      ...VIEW,
      fields: [
        ...VIEW.fields,
        {
          name: "eval_container_exec",
          field_type: "bool",
          current_value: false,
          default_value: false,
          description: "Whether the process runs inside the eval container.",
          group: "Agent",
          capability: "agent-runner",
          scope: "agent",
          restart_required: "",
          writable: false,
          sensitive: false,
          env_var: "AVA_CONTAINER_EXEC",
          remote_writable: false,
          per_agent: false,
          can_enable: null,
          reason: null,
        },
      ],
    };
    vi.spyOn(api, "getConfig").mockResolvedValue(viewWithEvalField);
    await renderSettled();
    expect(screen.queryByText("CONTAINER EXEC")).toBeNull();
    expect(screen.queryByTitle("AVA_CONTAINER_EXEC")).toBeNull();
  });

  it("field appears under its display-group header", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    // "LLM settings" group header appears before LOG LEVEL in the DOM.
    const groupHeader = screen.getByText("LLM settings");
    const fieldName = screen.getByText("LOG LEVEL");
    expect(
      groupHeader.compareDocumentPosition(fieldName) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("scope badge is shown per row (raw scope)", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    // Three fields have cluster-default scope (log_level, max_retries,
    // nudge_cadence) — the badge shows the scope verbatim.
    expect(inGroups("cluster-default")).toHaveLength(3);
    // "host" scope badge (on host_label, build_sha, browser_enabled, gateway_port).
    expect(inGroups("host")).toHaveLength(4);
    // "agent" scope badge (on enable_compact).
    expect(inGroups("agent")).toHaveLength(1);
    // "cluster-pinned" for api_key.
    expect(inGroups("cluster-pinned")).toHaveLength(1);
  });

  it("per-agent tag follows the wire field; Startup tag follows restart_required", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    // per_agent rides on the wire (ConfigFieldView.per_agent — the same flag
    // the per-model view uses), not a scope approximation: the three fixture
    // cluster-default fields carry per_agent=true. Counted inside the group
    // list: the per-model panel draws its own per-agent badge.
    expect(inGroups("per-agent")).toHaveLength(3);
    // log_level restarts "all"; nudge_cadence restarts "agent".
    expect(screen.getByText("Startup: all")).toBeTruthy();
    expect(screen.getAllByText(/Startup: /).length).toBeGreaterThanOrEqual(2);
  });

  it("sensitive field masked + lock; writable secret is write-only replaceable", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi.spyOn(api, "putConfig").mockResolvedValue(OK_RESULT(["api_key"]));
    await renderSettled();
    // Masked — the cleartext is never rendered.
    expect(screen.getByText("••••••••")).toBeTruthy();
    expect(screen.queryByText("secret-xyz")).toBeNull();
    expect(screen.getByTestId("sensitive-api_key")).toBeTruthy();
    // A writable secret exposes a Replace affordance that opens an EMPTY password
    // input (never seeded with the mask or a read-back value).
    fireEvent.click(screen.getByTestId("edit-api_key"));
    const input = screen.getByTestId<HTMLInputElement>("input-api_key");
    expect(input.getAttribute("type")).toBe("password");
    expect(input.value).toBe("");
    // A non-empty value replaces the secret; the delta carries the new value.
    fireEvent.change(input, { target: { value: "sk-NEW" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith({ log_level: "INFO", api_key: "sk-NEW" }, undefined),
    );
  });

  it("sensitive field: an empty submit keeps the secret (no PUT)", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi.spyOn(api, "putConfig").mockResolvedValue(OK_RESULT([]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-api_key"));
    fireEvent.click(screen.getByTestId("edit-api_key"));
    screen.getByTestId("input-api_key"); // opened, left blank
    fireEvent.click(screen.getByTestId("save-api_key"));
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("read-only field tagged CLI-only + no edit button", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    expect(screen.getAllByText("CLI-only").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("edit-build_sha")).toBeNull();
  });

  it("hides non-actionable fields matched by env var (cluster secret)", async () => {
    const withHidden = {
      ...VIEW,
      fields: [
        ...VIEW.fields,
        {
          name: "cluster_secret",
          field_type: "string" as const,
          current_value: "••••••••",
          default_value: "",
          description: "Cluster pre-shared secret",
          group: "Data plane",
          capability: "gateway" as const,
          scope: "cluster-pinned" as const,
          restart_required: "all" as const,
          writable: true,
          sensitive: true,
          env_var: "AVA_CLUSTER_SECRET",
          remote_writable: false,
          per_agent: false,
          can_enable: null,
          reason: null,
        },
      ],
    };
    vi.spyOn(api, "getConfig").mockResolvedValue(withHidden);
    await renderSettled();
    // AVA_CLUSTER_SECRET is on the hidden list → its row never renders.
    expect(screen.queryByText("CLUSTER SECRET")).toBeNull();
  });

  it("bool field renders a Switch reflecting the current value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("toggle-enable_compact"));
    expect(
      screen.getByTestId("toggle-enable_compact").getAttribute("aria-checked"),
    ).toBe("true");
  });
});

describe("ConfigPage tag filter", () => {
  it("filtering by Sensitive hides non-matching rows and empty groups", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    fireEvent.click(screen.getByTestId("filter-sensitive"));
    // Only api_key is sensitive → its group survives, the LLM group vanishes.
    expect(screen.getByText("API KEY")).toBeTruthy();
    expect(screen.queryByText("LOG LEVEL")).toBeNull();
    expect(screen.queryByText("LLM settings")).toBeNull();
    expect(screen.getByText("1 setting")).toBeTruthy();
    // Back to All restores everything.
    fireEvent.click(screen.getByTestId("filter-all"));
    expect(screen.getByText("LOG LEVEL")).toBeTruthy();
  });

  it("filtering by CLI-only keeps only non-editable rows", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    fireEvent.click(screen.getByTestId("filter-cli-only"));
    // build_sha (writable=false) + gateway_port (writable=false). api_key is a
    // WRITABLE secret → editable write-only → "runtime", not "cli-only".
    expect(screen.getByText("BUILD SHA")).toBeTruthy();
    expect(screen.getByText("GATEWAY PORT")).toBeTruthy();
    expect(screen.queryByText("API KEY")).toBeNull();
    expect(screen.queryByText("LOG LEVEL")).toBeNull();
  });
});

describe("ConfigPage capability visibility", () => {
  it("Cluster view shows every capability's fields", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    // gateway_port (capability gateway) renders under its display group.
    expect(
      screen.getByTestId("config-group-config-connection").textContent,
    ).toMatch(/GATEWAY PORT/);
  });

  it("remote agent-runner view drops gateway-capability fields, keeps the rest", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    // The gateway-capability field vanishes with its now-empty group
    // (machine-independent — edited on the Cluster view).
    await waitFor(() => screen.getByText("HOST LABEL"));
    expect(screen.queryByText("GATEWAY PORT")).toBeNull();
    expect(screen.queryByTestId("config-group-config-connection")).toBeNull();
    // agent-runner + common capability fields are still present.
    expect(screen.getByText("LOG LEVEL")).toBeTruthy();
  });

  it("remote co-located gateway,agent-runner box keeps gateway fields", async () => {
    // A single box carries both capabilities, so its remote view still shows
    // gateway-capability fields — its gateway-daemon toggles must stay editable.
    vi.spyOn(api, "getConfig").mockResolvedValue({
      ...VIEW,
      machine_capabilities: ["gateway", "agent-runner"],
    });
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByText("HOST LABEL"));
    expect(screen.getByText("GATEWAY PORT")).toBeTruthy();
  });
});

describe("ConfigPage bool toggle (putConfig + write result)", () => {
  it("click toggle → putConfig with raw_overrides delta", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi
      .spyOn(api, "putConfig")
      .mockResolvedValue(OK_RESULT(["enable_compact"]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("toggle-enable_compact"));
    fireEvent.click(screen.getByTestId("toggle-enable_compact"));
    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    expect(putSpy).toHaveBeenCalledWith(
      { log_level: "INFO", enable_compact: false },
      undefined,
    );
  });

  it("putConfig fails → show saveError", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    vi.spyOn(api, "putConfig").mockRejectedValue(new Error("server 500"));
    wrap(<ConfigPage />);
    const btn = await waitFor(() =>
      screen.getByTestId("toggle-enable_compact"),
    );
    fireEvent.click(btn);
    await waitFor(() => screen.getByText(/Save failed/));
    expect(screen.getByText(/server 500/)).toBeTruthy();
  });
});

describe("ConfigPage enum select", () => {
  it("enum field renders a select of its choices with the current value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByTestId<HTMLSelectElement>("select-nudge_cadence"),
    );
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "once_per_compaction",
      "every_time",
    ]);
    expect(select.value).toBe("once_per_compaction");
  });

  it("selecting a choice PUTs the delta immediately", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi
      .spyOn(api, "putConfig")
      .mockResolvedValue(OK_RESULT(["nudge_cadence"], ["agent"]));
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByTestId<HTMLSelectElement>("select-nudge_cadence"),
    );
    fireEvent.change(select, { target: { value: "every_time" } });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "INFO", nudge_cadence: "every_time" },
        undefined,
      ),
    );
  });

  it("enum field is read-only text (no select) on a remote machine view", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const machineSelect = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(machineSelect, { target: { value: AGENT_RUNNER } });
    // The cluster-scope enum is machine-independent → read-only here, so it
    // renders its value as text with no editable <select>.
    await waitFor(() => expect(screen.getByText("once_per_compaction")).toBeTruthy());
    expect(screen.queryByTestId("select-nudge_cadence")).toBeNull();
  });
});

describe("ConfigPage text inline edit", () => {
  it("click edit enters edit mode → input has current value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    expect((input as HTMLInputElement).value).toBe("INFO");
  });

  it("Enter commits → putConfig uses new value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi
      .spyOn(api, "putConfig")
      .mockResolvedValue(OK_RESULT(["log_level"]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    fireEvent.change(input, { target: { value: "DEBUG" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "DEBUG" },
        undefined,
      ),
    );
  });

  it("Escape cancels → putConfig not called", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi
      .spyOn(api, "putConfig")
      .mockResolvedValue(OK_RESULT([]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    fireEvent.change(input, { target: { value: "DEBUG" } });
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByTestId("input-log_level")).toBeNull(),
    );
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("unchanged value commit → cancels, putConfig not called", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi
      .spyOn(api, "putConfig")
      .mockResolvedValue(OK_RESULT([]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("save-log_level"));
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("int field edit → putConfig receives parseInt-converted number", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi
      .spyOn(api, "putConfig")
      .mockResolvedValue(OK_RESULT(["max_retries"]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-max_retries"));
    fireEvent.click(screen.getByTestId("edit-max_retries"));
    const input = screen.getByTestId("input-max_retries");
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("save-max_retries"));
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "INFO", max_retries: 5 },
        undefined,
      ),
    );
    const callArg = putSpy.mock.calls[0]?.[0];
    expect(typeof callArg.max_retries).toBe("number");
  });
});

describe("ConfigPage restart banner from write result", () => {
  it("no banner before any write; appears after write with restart targets", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    vi.spyOn(api, "putConfig").mockResolvedValue(
      OK_RESULT(["log_level"], ["all"]),
    );
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    expect(
      screen.queryByText(/restart the following processes/),
    ).toBeNull();
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    fireEvent.change(input, { target: { value: "DEBUG" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const banner = await waitFor(() =>
      screen.getByText(/restart the following processes/i),
    );
    expect(banner.textContent).toMatch(/All processes/);
  });
});

describe("ConfigPage machine selector", () => {
  it("selector renders Cluster + every online machine, not offline", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toEqual(["", GATEWAY, AGENT_RUNNER]);
    expect(select.options[0].textContent).toMatch(/Cluster/);
    expect(select.options[0].textContent).toMatch(new RegExp(GATEWAY));
  });

  it("selecting agent-runner refetches getConfig with machine name", async () => {
    const getSpy = vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    await waitFor(() => expect(getSpy).toHaveBeenCalledWith(undefined));
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() =>
      expect(getSpy).toHaveBeenCalledWith(AGENT_RUNNER),
    );
  });

  it("agent-runner view: host remote_writable field editable, cluster field read-only", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByTestId("edit-host_label"));
    expect(screen.queryByTestId("edit-log_level")).toBeNull();
  });

  it("agent-runner view: can_enable=false bool is disabled with reason", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    const toggle = await waitFor(
      () => screen.getByTestId<HTMLButtonElement>("toggle-browser_enabled"),
    );
    expect(toggle.disabled).toBe(true);
    expect(
      screen.getByTestId("capability-browser_enabled").textContent,
    ).toMatch(/no display detected/);
  });

  it("remote view shows edit-on-Cluster hint for non-host fields", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByText("HOST LABEL"));
    expect(
      screen.getAllByText(/edit on Cluster view/i).length,
    ).toBeGreaterThan(0);
  });

  it("PUT result with !ok field shows reason and reverts value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi.spyOn(api, "putConfig").mockResolvedValue({
      applied: false,
      results: {
        host_label: { ok: false, reason: "value rejected by host" },
      },
      restart_required: [],
    });
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByTestId("edit-host_label"));
    fireEvent.click(screen.getByTestId("edit-host_label"));
    const input = screen.getByTestId("input-host_label");
    fireEvent.change(input, { target: { value: "rack-Z" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "INFO", host_label: "rack-Z" },
        AGENT_RUNNER,
      ),
    );
    await waitFor(() => screen.getByTestId("error-host_label"));
    expect(
      screen.getByTestId("error-host_label").textContent,
    ).toMatch(/value rejected by host/);
    await waitFor(() => screen.getByText("rack-A"));
    expect(screen.queryByText("rack-Z")).toBeNull();
  });
});

describe("Fix 1 — onSettled invalidates written machine's query key", () => {
  it("toggling cluster then switching machines invalidates cluster key only", async () => {
    let resolvePut!: (v: ConfigWriteResult) => void;
    const putInflight = new Promise<ConfigWriteResult>((res) => {
      resolvePut = res;
    });
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    vi.spyOn(api, "putConfig").mockReturnValue(putInflight);

    const qc = makeQc();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    wrap(<ConfigPage />, qc);

    const toggle = await waitFor(() =>
      screen.getByTestId("toggle-enable_compact"),
    );
    fireEvent.click(toggle);

    const select = screen.getByLabelText<HTMLSelectElement>("Machine");
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });

    resolvePut({
      applied: true,
      results: { enable_compact: { ok: true, reason: null } },
      restart_required: [],
    });

    await waitFor(() => {
      const invalidatedKeys = invalidateSpy.mock.calls.map(
        ([opts]) => opts?.queryKey,
      );
      const hasCluster = invalidatedKeys.some(
        (k) =>
          Array.isArray(k) && k[0] === "config" && k[1] === null,
      );
      expect(hasCluster).toBe(true);
    });

    const invalidatedKeys = invalidateSpy.mock.calls.map(
      ([opts]) => opts?.queryKey,
    );
    const hasWorker = invalidatedKeys.some(
      (k) =>
        Array.isArray(k) &&
        k[0] === "config" &&
        k[1] === AGENT_RUNNER,
    );
    expect(hasWorker).toBe(false);
  });
});

describe("Fix 2 — capability reason hidden on non-editable rows", () => {
  it("cluster-scope bool with can_enable=false on agent-runner: reason NOT shown", async () => {
    const clusterBoolGated: ConfigView = {
      fields: [
        {
          name: "cluster_flag",
          field_type: "bool",
          current_value: false,
          default_value: false,
          description: "A cluster-pinned bool",
          group: "core",
          capability: "common",
          scope: "cluster-pinned",
          restart_required: "",
          writable: false,
          sensitive: false,
          env_var: "AVA_CLUSTER_FLAG",
          remote_writable: false,
          per_agent: false,
          can_enable: false,
          reason: "should not appear",
        },
      ],
      raw_overrides: {},
      machine_capabilities: ["agent-runner"],
    };
    vi.spyOn(api, "getConfig").mockResolvedValue(clusterBoolGated);

    wrap(<ConfigPage />);

    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByText("CLUSTER FLAG"));

    expect(screen.queryByTestId("capability-cluster_flag")).toBeNull();
    expect(screen.queryByText("should not appear")).toBeNull();
  });
});

// The read-only per-model view: it must name the layer that produced each
// effective value (not just print the value), and it must be able to hand the
// user off to the one editor that CAN change it — including when the current
// filter has that row hidden, which is the failure mode a plain #anchor has.
describe("ConfigPage per-model resolution view", () => {
  it("labels each row with the layer that produced its value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-source-auto_compact_fraction"));

    expect(screen.getByTestId("per-model-source-auto_compact_fraction").textContent).toBe(
      "model default",
    );
    expect(screen.getByTestId("per-model-source-max_retries").textContent).toBe(".env");
    expect(screen.getByTestId("per-model-source-llm_retry_max_attempts").textContent).toBe(
      "shared default",
    );
    expect(screen.getByTestId("per-model-value-auto_compact_fraction").textContent).toBe(
      "0.55",
    );
  });

  it("shows the layers a value shadowed, and nothing when the floor itself won", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-shadowed-max_retries"));

    // Explicit shadows both the model default and the shared floor.
    expect(screen.getByTestId("per-model-shadowed-max_retries").textContent).toBe(
      "shadows model default 4, shared default 6",
    );
    // A model default shadows only the floor.
    expect(screen.getByTestId("per-model-shadowed-auto_compact_fraction").textContent).toBe(
      "shadows shared default 0.8",
    );
    // The floor winning shadows nothing — the badge already says so.
    expect(screen.queryByTestId("per-model-shadowed-llm_retry_max_attempts")).toBeNull();
  });

  it("re-resolves when another model is picked", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-source-max_retries"));
    // Defaults to the cluster's own model, not the first option.
    const select = screen.getByLabelText<HTMLSelectElement>("Model");
    expect(select.value).toBe("deepseek-v4-pro");

    fireEvent.change(select, { target: { value: "claude-opus-5" } });
    await waitFor(() =>
      expect(vi.mocked(api.getResolvedConfig)).toHaveBeenCalledWith("claude-opus-5"),
    );
  });

  it("row link reveals its editor even when the filter had it hidden", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-goto-max_retries"));

    // Hide max_retries behind a filter it doesn't match. Assert on the row
    // element, not its label text — the per-model row carries the same label.
    fireEvent.click(screen.getByTestId("filter-sensitive"));
    await waitFor(() =>
      expect(document.getElementById("config-field-max_retries")).toBeNull(),
    );

    fireEvent.click(screen.getByTestId("per-model-goto-max_retries"));

    const row = await waitFor(() => {
      const el = document.getElementById("config-field-max_retries");
      expect(el).not.toBeNull();
      return el;
    });
    expect(row?.className).toContain("ring-primary");
    // Only the linked row is highlighted.
    expect(document.getElementById("config-field-log_level")?.className).not.toContain(
      "ring-primary",
    );
  });

  it("flags a model that is not in the registry", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    vi.spyOn(api, "getResolvedConfig").mockResolvedValue({
      ...RESOLVED,
      model: "some-unknown-model",
      registered: false,
    });
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-unregistered"));
  });
});
