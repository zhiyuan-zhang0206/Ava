

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/transport/api";
import type {
  AgentMachineRow,
  ConfigView,
  ConfigWriteResult,
  ModelsResponse,
  ResolvedConfigView,
  SystemStatus,
} from "@/lib/contracts/types";

import ConfigPage from "./page";

const localStorageMock = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
  };
})();
Object.defineProperty(globalThis, "localStorage", {
  value: localStorageMock,
  writable: true,
});

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

async function renderSettled() {
  const rendered = wrap(<ConfigPage />);
  await screen.findByText("LOG LEVEL");

  await screen.findByTestId("per-model-source-auto_compact_fraction");
  return rendered;
}

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
      env_var: "MEIVA_LOG_LEVEL",
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
      env_var: "MEIVA_MAX_RETRIES",
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
      env_var: "MEIVA_ENABLE_COMPACT",
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
      env_var: "MEIVA_API_KEY",
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
      env_var: "MEIVA_BUILD_SHA",
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
      env_var: "MEIVA_HOST_LABEL",
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
      env_var: "MEIVA_BROWSER_ENABLED",
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
      env_var: "MEIVA_NUDGE_CADENCE",
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
      env_var: "MEIVA_GATEWAY_PORT",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
  ],
  raw_overrides: { log_level: "INFO" },

  machine_capabilities: ["agent-runner"],
};

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
      env_var: "MEIVA_AUTO_COMPACT_FRACTION",
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
      env_var: "MEIVA_MAX_RETRIES",
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
      env_var: "MEIVA_LLM_RETRY_MAX_ATTEMPTS",
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
  localStorage.clear();
  vi.spyOn(api, "getMachines").mockResolvedValue(MACHINES);
  vi.spyOn(api, "getSystemStatus").mockResolvedValue(STATUS);
  vi.spyOn(api, "getModels").mockResolvedValue(MODELS);
  vi.spyOn(api, "getResolvedConfig").mockResolvedValue(RESOLVED);

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
  it("collapses groups on demand and restores the per-device state", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();

    const heading = screen.getByRole("heading", { name: /LLM settings/ });
    const toggle = within(heading).getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(document.getElementById("config-field-log_level")).toBeNull();
    expect(JSON.parse(localStorage.getItem("control.config.collapsedGroups")!)).toContain(
      "config-llm",
    );

    cleanup();
    wrap(<ConfigPage />);
    const restoredHeading = await screen.findByRole("heading", { name: /LLM settings/ });
    await waitFor(() =>
      expect(within(restoredHeading).getByRole("button").getAttribute("aria-expanded")).toBe(
        "false",
      ),
    );
    expect(document.getElementById("config-field-log_level")).toBeNull();
  });

  it("ignores malformed collapsed-group storage", async () => {
    localStorage.setItem("control.config.collapsedGroups", "not-json");
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);

    await renderSettled();
    expect(document.getElementById("config-field-log_level")).not.toBeNull();
  });

  it("display-group headers render with raw case-preserved field labels", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();

    expect(screen.getByText("LLM settings")).toBeTruthy();
    expect(screen.getByText("Agent execution")).toBeTruthy();
    expect(screen.getByText("Display & general")).toBeTruthy();
    expect(screen.getByText("Connection")).toBeTruthy();

    expect(screen.getByTitle("MEIVA_LOG_LEVEL")).toBeTruthy();

    expect(inGroups("MAX RETRIES")).toHaveLength(1);
    expect(screen.queryByText("log_level")).toBeNull();
    expect(screen.queryByText("MEIVA_LOG_LEVEL")).toBeNull();
  });

  it("HIDDEN_ENV_VARS: transport micro-tuning is dropped from the panel", async () => {

    const viewWithHiddenField: ConfigView = {
      ...VIEW,
      fields: [
        ...VIEW.fields,
        {
          name: "gateway_max_retries",
          field_type: "int",
          current_value: 3,
          default_value: 3,
          description: "SDK to gateway transport retry count.",
          group: "Gateway",
          capability: "agent-runner",
          scope: "agent",
          restart_required: "",
          writable: false,
          sensitive: false,
          env_var: "MEIVA_GATEWAY_MAX_RETRIES",
          remote_writable: false,
          per_agent: false,
          can_enable: null,
          reason: null,
        },
      ],
    };
    vi.spyOn(api, "getConfig").mockResolvedValue(viewWithHiddenField);
    await renderSettled();
    expect(screen.queryByText("GATEWAY MAX RETRIES")).toBeNull();
    expect(screen.queryByTitle("MEIVA_GATEWAY_MAX_RETRIES")).toBeNull();
  });

  it("field appears under its display-group header", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();

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

    expect(inGroups("cluster-default")).toHaveLength(3);

    expect(inGroups("host")).toHaveLength(4);

    expect(inGroups("agent")).toHaveLength(1);

    expect(inGroups("cluster-pinned")).toHaveLength(1);
  });

  it("per-agent tag follows the wire field; Startup tag follows restart_required", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();

    expect(inGroups("per-agent")).toHaveLength(3);

    expect(screen.getByText("Startup: all")).toBeTruthy();
    expect(screen.getAllByText(/Startup: /).length).toBeGreaterThanOrEqual(2);
  });

  it("sensitive field masked + lock; writable secret is write-only replaceable", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    const putSpy = vi.spyOn(api, "putConfig").mockResolvedValue(OK_RESULT(["api_key"]));
    await renderSettled();

    expect(screen.getByText("••••••••")).toBeTruthy();
    expect(screen.queryByText("secret-xyz")).toBeNull();
    expect(screen.getByTestId("sensitive-api_key")).toBeTruthy();

    fireEvent.click(screen.getByTestId("edit-api_key"));
    const input = screen.getByTestId<HTMLInputElement>("input-api_key");
    expect(input.getAttribute("type")).toBe("password");
    expect(screen.getByLabelText("Replace MEIVA_API_KEY")).toBe(input);
    expect(input.value).toBe("");

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
    screen.getByTestId("input-api_key");
    fireEvent.click(screen.getByTestId("save-api_key"));
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("read-only field tagged CLI-only + no edit button", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    expect(screen.getAllByText("CLI-only").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("edit-build_sha")).toBeNull();
    expect(within(screen.getByTitle("MEIVA_BUILD_SHA").closest("[id^='config-field-']")!).queryByText(/read-only/i)).toBeNull();
  });

  it("distinguishes editable values from runtime fields read-only on this machine", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) =>
        field.name === "host_label" ? { ...field, remote_writable: false } : field,
      ),
    });
    wrap(<ConfigPage />);

    const editableValue = await screen.findByTitle("INFO");
    expect(editableValue.className).toContain("hover:bg-muted/50");
    expect(editableValue.className).toContain("cursor-text");

    fireEvent.change(screen.getByLabelText("Machine"), {
      target: { value: AGENT_RUNNER },
    });
    const hostLabel = await screen.findByTitle("MEIVA_HOST_LABEL");
    await waitFor(() => expect(screen.queryByTestId("edit-host_label")).toBeNull());
    const hostRow = hostLabel.closest<HTMLElement>("[id^='config-field-']")!;
    expect(within(hostRow).getByText("Runtime")).toBeTruthy();
    expect(within(hostRow).getByText(/read-only/i)).toBeTruthy();
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
          env_var: "MEIVA_CLUSTER_SECRET",
          remote_writable: false,
          per_agent: false,
          can_enable: null,
          reason: null,
        },
      ],
    };
    vi.spyOn(api, "getConfig").mockResolvedValue(withHidden);
    await renderSettled();

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

    expect(screen.getByText("API KEY")).toBeTruthy();
    expect(screen.queryByText("LOG LEVEL")).toBeNull();
    expect(screen.queryByText("LLM settings")).toBeNull();
    expect(screen.getByText("1 setting")).toBeTruthy();

    fireEvent.click(screen.getByTestId("filter-all"));
    expect(screen.getByText("LOG LEVEL")).toBeTruthy();
  });

  it("filtering by CLI-only keeps only non-editable rows", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    await renderSettled();
    fireEvent.click(screen.getByTestId("filter-cli-only"));

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

    await waitFor(() => screen.getByText("HOST LABEL"));
    expect(screen.queryByText("GATEWAY PORT")).toBeNull();
    expect(screen.queryByTestId("config-group-config-connection")).toBeNull();

    expect(screen.getByText("LOG LEVEL")).toBeTruthy();
  });

  it("remote co-located gateway,agent-runner box keeps gateway fields", async () => {

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
    expect(screen.getAllByText(/server 500/).length).toBeGreaterThan(0);
  });
  it("splits owners and retains a committed restart when another owner fails", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) => ({ ...field, owner: field.name === "enable_compact" ? "meiva_fleet" : null })),
    });
    const put = vi.spyOn(api, "putConfig").mockResolvedValueOnce(OK_RESULT(["log_level"], ["all"])).mockRejectedValueOnce(new Error("Fleet image changed"));
    await renderSettled();
    fireEvent.click(screen.getByTestId("toggle-enable_compact"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(put.mock.calls).toEqual([[{ log_level: "INFO" }, undefined], [{ enable_compact: false }, undefined]]);
    expect(await screen.findByText(/Save failed/)).toBeTruthy();
    expect(screen.getByText(/Fleet image changed/)).toBeTruthy();
    expect(screen.getByText(/restart the following processes/i).textContent).toMatch(/All processes/);
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

  it("shows a disabled not-set placeholder for an empty enum value", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) =>
        field.name === "nudge_cadence" ? { ...field, current_value: null } : field,
      ),
    });
    wrap(<ConfigPage />);

    const select = await screen.findByTestId<HTMLSelectElement>("select-nudge_cadence");
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toBe("— not set —");
    expect(select.options[0].disabled).toBe(true);
  });

  it("enum field is read-only text (no select) on a remote machine view", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const machineSelect = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(machineSelect, { target: { value: AGENT_RUNNER } });

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
          env_var: "MEIVA_CLUSTER_FLAG",
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
    expect(screen.getByTestId("per-model-source-auto_compact_fraction").className).toContain(
      "border-dashed",
    );
    expect(screen.getByTestId("per-model-source-auto_compact_fraction").className).toContain(
      "text-muted-foreground/80",
    );
  });

  it("shows a disabled loading placeholder before models resolve", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    vi.spyOn(api, "getModels").mockReturnValue(new Promise(() => undefined));
    wrap(<ConfigPage />);

    await screen.findByText("LOG LEVEL");
    const select = screen.getByLabelText<HTMLSelectElement>("Model");
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toBe("Loading…");
    expect(select.options[0].disabled).toBe(true);
  });

  it("shows the layers a value shadowed, and nothing when the floor itself won", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-shadowed-max_retries"));

    expect(screen.getByTestId("per-model-shadowed-max_retries").textContent).toBe(
      "shadows model default 4, shared default 6",
    );

    expect(screen.getByTestId("per-model-shadowed-auto_compact_fraction").textContent).toBe(
      "shadows shared default 0.8",
    );

    expect(screen.queryByTestId("per-model-shadowed-llm_retry_max_attempts")).toBeNull();
  });

  it("re-resolves when another model is picked", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-source-max_retries"));

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

    expect(document.getElementById("config-field-log_level")?.className).not.toContain(
      "ring-primary",
    );
  });

  it("row link expands a persisted-collapsed group before revealing its editor", async () => {
    localStorage.setItem(
      "control.config.collapsedGroups",
      JSON.stringify(["config-llm"]),
    );
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await screen.findByTestId("per-model-goto-max_retries");
    await waitFor(() =>
      expect(document.getElementById("config-field-max_retries")).toBeNull(),
    );

    fireEvent.click(screen.getByTestId("per-model-goto-max_retries"));

    await waitFor(() =>
      expect(document.getElementById("config-field-max_retries")).not.toBeNull(),
    );
    expect(JSON.parse(localStorage.getItem("control.config.collapsedGroups")!)).not.toContain(
      "config-llm",
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

describe("ConfigPage without a model catalog", () => {
  it("reports model failure while preserving ordinary configuration editing", async () => {
    vi.spyOn(api, "getConfig").mockResolvedValue(VIEW);
    vi.spyOn(api, "getModels").mockRejectedValue(new Error("provider private diagnostic"));
    vi.spyOn(api, "getDefaultModel").mockRejectedValue(new Error("model unavailable"));
    const put = vi.spyOn(api, "putConfig").mockResolvedValue(OK_RESULT(["log_level"]));
    wrap(<ConfigPage />);
    await screen.findByText("Model settings are unavailable. Check the gateway configuration and reload.");
    expect(screen.getByLabelText<HTMLSelectElement>("Model").disabled).toBe(true);
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.queryByText(/provider private diagnostic/)).toBeNull();
    expect(api.getResolvedConfig).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByTestId("edit-log_level"));
    fireEvent.change(screen.getByTestId("input-log_level"), { target: { value: "DEBUG" } });
    fireEvent.keyDown(screen.getByTestId("input-log_level"), { key: "Enter" });
    await waitFor(() => expect(put).toHaveBeenCalledWith({ log_level: "DEBUG" }, undefined));
  });
});
