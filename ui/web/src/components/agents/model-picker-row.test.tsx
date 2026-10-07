import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "../../../messages/en/agents/spawn.json";
import { ModelPickerRow } from "./model-picker-row";

describe("model picker TPS column", () => {
  it("shows a sourced vendor qualifier beside the price and selects the model", () => {
    const select = vi.fn();
    render(
      <NextIntlClientProvider locale="en" messages={{ spawn: messages }}>
        <ModelPickerRow
          model="gpt-5.6-sol-fast"
          selected={false}
          onSelect={select}
          info={{
            provider: "gpt",
            context_window: 1_000_000,
            pricing: { input: 8, cache_read: 0.8, output: 40 },
            reference_tps: {
              display: ">80",
              source_url: "https://openai.com/api-fast-mode/",
              source_checked_at: "2026-10-07",
              note: "Enterprise Fast latency SLA",
            },
          }}
        />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText(">80").title).toContain("Enterprise");
    expect(screen.getByText(">80").title).toContain("https://openai.com/api-fast-mode/");
    expect(screen.getByText(/\$8.00.*\$0.80.*\$40.00/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    expect(select).toHaveBeenCalledOnce();
  });

  it("uses a dash when no vendor reference is available", () => {
    render(
      <NextIntlClientProvider locale="en" messages={{ spawn: messages }}>
        <ModelPickerRow model="mimo-v2.6-pro-ultraspeed" selected={false} onSelect={vi.fn()} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTitle("No reliable vendor-published output TPS").textContent).toBe("—");
  });

  it("keeps small rates visible and explains the three prices", () => {
    render(
      <NextIntlClientProvider locale="en" messages={{ spawn: messages }}>
        <ModelPickerRow model="deepseek-flash" selected={false} onSelect={vi.fn()}
          info={{ provider: "deepseek", context_window: 1_000_000,
            pricing: { input: 0.33, cache_read: 0.003, output: 0.99 } }} />
      </NextIntlClientProvider>,
    );
    const prices = screen.getByText(/\$0.33.*\$0.003.*\$0.99/);
    expect(prices.title).toContain("Input / cache read / output");
    expect(prices.title).toContain("Longer contexts");
  });

  it("includes declared cache-write rates in the price tooltip", () => {
    render(
      <NextIntlClientProvider locale="en" messages={{ spawn: messages }}>
        <ModelPickerRow model="claude-opus-5-5" selected={false} onSelect={vi.fn()}
          info={{ provider: "claude", context_window: 1_000_000,
            pricing: { input: 4, cache_read: 0.2, output: 20,
              cache_write_5m: 5, cache_write_1h: 8 } }} />
      </NextIntlClientProvider>,
    );
    const prices = screen.getByText(/\$4.00.*\$0.20.*\$20.00/);
    expect(prices.title).toContain("Cache write (5m): $5.00 / 1M");
    expect(prices.title).toContain("Cache write (1h): $8.00 / 1M");
  });
});
