import { describe, expect, it } from "vitest";

import { groupedModels, isSuperseded, providerLabel } from "./models";
import type { ModelsResponse } from "./types";

function modelsResponse(
  providers: Record<string, string[]>,
  inputPrices: Record<string, number | undefined> = {},
): ModelsResponse {
  const models = Object.fromEntries(
    Object.entries(providers).flatMap(([provider, providerModels]) =>
      providerModels.map((model) => {
        const input = inputPrices[model];
        return [
          model,
          {
            provider,
            context_window: 128_000,
            ...(input === undefined
              ? {}
              : { pricing: { input, cache_read: input / 10, output: input * 2 } }),
          },
        ];
      }),
    ),
  );
  return { providers, models, default: "" };
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
  it("keeps API provider order while sorting each provider by input price descending", () => {
    const data = modelsResponse(
      {
        claude: ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"],
        deepseek: ["deepseek-v4-flash", "deepseek-v4-pro"],
      },
      {
        "claude-haiku-4-5-20251001": 0.2,
        "claude-sonnet-5": 2.0,
        "claude-opus-5": 5.0,
        "deepseek-v4-flash": 0.1,
        "deepseek-v4-pro": 0.5,
      },
    );
    expect(groupedModels(data)).toEqual([
      ["claude", ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]],
      ["deepseek", ["deepseek-v4-pro", "deepseek-v4-flash"]],
    ]);
  });

  it("puts models with missing pricing after priced models", () => {
    const data = modelsResponse(
      { claude: ["claude-unpriced", "claude-haiku", "claude-sonnet"] },
      { "claude-haiku": 0.2, "claude-sonnet": 2.0 },
    );

    expect(groupedModels(data)).toEqual([
      ["claude", ["claude-sonnet", "claude-haiku", "claude-unpriced"]],
    ]);
  });

  it("preserves API order for models with equal prices", () => {
    const data = modelsResponse(
      { claude: ["claude-first", "claude-second", "claude-third"] },
      { "claude-first": 2.0, "claude-second": 2.0, "claude-third": 2.0 },
    );

    expect(groupedModels(data)).toEqual([
      ["claude", ["claude-first", "claude-second", "claude-third"]],
    ]);
  });

  it("returns an empty list when modelsData is undefined", () => {
    expect(groupedModels(undefined)).toEqual([]);
  });

  it("filters before sorting and drops providers left with no models", () => {
    const data = modelsResponse(
      {
        claude: ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"],
        deepseek: ["deepseek-v4-pro"],
      },
      {
        "claude-haiku-4-5-20251001": 0.2,
        "claude-opus-5": 5.0,
        "claude-sonnet-5": 2.0,
        "deepseek-v4-pro": 0.5,
      },
    );
    const result = groupedModels(data, (m) => m !== "claude-haiku-4-5-20251001" && m !== "deepseek-v4-pro");
    expect(result).toEqual([["claude", ["claude-opus-5", "claude-sonnet-5"]]]);
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

describe("isSuperseded", () => {
  const response: ModelsResponse = {
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
      "deepseek-v4-legacy": {
        provider: "deepseek",
        context_window: 128_000,
      },
    },
    default: "deepseek-v4-flash",
  };

  it("returns true when the registry supplies a replacement", () => {
    expect(isSuperseded(response, "deepseek-v4-pro")).toBe(true);
  });

  it("returns false for null or omitted replacements", () => {
    expect(isSuperseded(response, "deepseek-v4-flash")).toBe(false);
    expect(isSuperseded(response, "deepseek-v4-legacy")).toBe(false);
  });

  it("returns false when the model metadata or catalog is absent", () => {
    expect(isSuperseded(response, "deepseek-v4-unknown")).toBe(false);
    expect(isSuperseded(undefined, "deepseek-v4-pro")).toBe(false);
  });
});
