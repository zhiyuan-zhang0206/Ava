import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WindowSelect } from "./window-select";

const OPTIONS = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "all", label: "All" },
];

describe("WindowSelect", () => {
  it("renders the option set and reports changes by value", () => {
    const onChange = vi.fn();
    render(
      <WindowSelect value="24h" options={OPTIONS} onChange={onChange} ariaLabel="Window" />,
    );
    const sel = screen.getByLabelText<HTMLSelectElement>("Window");
    expect(sel.value).toBe("24h");
    expect([...sel.options].map((o) => o.value)).toEqual(["24h", "7d", "all"]);
    fireEvent.change(sel, { target: { value: "all" } });
    expect(onChange).toHaveBeenCalledWith("all");
  });
});
