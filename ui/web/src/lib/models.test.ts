import { describe, expect, it } from "vitest";

import { groupedModels, providerLabel } from "./models";
import type { ModelsResponse } from "./types";

function modelsResponse(providers: Record<string, string[]>): ModelsResponse {
  return { providers, models: {}, default: "" };
}

describe("providerLabel", () => {
  it("uses the conventional display name for a known provider", () => {
    expect(providerLabel("deepseek")).toBe("DeepSeek");
    expect(providerLabel("gpt")).toBe("GPT");
    expect(providerLabel("mimo")).toBe("MiMo");
    expect(providerLabel("glm")).toBe("GLM");
  });

  it("falls back to capitalizing an unrecognized provider", () => {
    expect(providerLabel("newprovider")).toBe("Newprovider");
  });

  it("handles an empty string without throwing", () => {
    expect(providerLabel("")).toBe("");
  });
});

describe("groupedModels", () => {
  it("groups in API provider + within-provider order", () => {
    const data = modelsResponse({
      claude: ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
      deepseek: ["deepseek-v4-pro", "deepseek-v4-flash"],
    });
    expect(groupedModels(data)).toEqual([
      ["claude", ["claude-sonnet-5", "claude-haiku-4-5-20251001"]],
      ["deepseek", ["deepseek-v4-pro", "deepseek-v4-flash"]],
    ]);
  });

  it("returns an empty list when modelsData is undefined", () => {
    expect(groupedModels(undefined)).toEqual([]);
  });

  it("applies a per-model filter and drops providers left with no models", () => {
    const data = modelsResponse({
      claude: ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
      deepseek: ["deepseek-v4-pro"],
    });
    const result = groupedModels(data, (m) => m !== "claude-haiku-4-5-20251001" && m !== "deepseek-v4-pro");
    expect(result).toEqual([["claude", ["claude-sonnet-5"]]]);
  });

  it("drops a provider entirely if every model is filtered out", () => {
    const data = modelsResponse({
      claude: ["claude-sonnet-5"],
      deepseek: ["deepseek-v4-pro"],
    });
    const result = groupedModels(data, (m) => m.startsWith("claude"));
    expect(result).toEqual([["claude", ["claude-sonnet-5"]]]);
  });
});
