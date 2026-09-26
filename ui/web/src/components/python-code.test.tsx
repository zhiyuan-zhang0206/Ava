import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const copy = vi.hoisted(() => vi.fn().mockResolvedValue(true));
vi.mock("@/lib/clipboard", () => ({ copyToClipboard: copy }));

import { PythonCode } from "./python-code";

it("renders highlighted code and its copy control on the first render", async () => {
  const first = 'print("<pending>")';
  const latest = `${first}\nprint(2)`;
  const { container, rerender } = render(<PythonCode code={first} streaming />);
  // No waitFor here: opening code must not require an asynchronous JS chunk.
  expect(container.querySelector("pre")?.textContent).toBe(first);
  expect(container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Copy code" })).toBeDefined();

  rerender(<PythonCode code={latest} streaming />);
  expect(container.querySelectorAll(".token-line")).toHaveLength(2);
  expect(container.querySelector("pre")?.textContent).toContain("print(2)");
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith(latest));

  rerender(<PythonCode code={latest} />);
  expect(container.querySelector("pre .animate-pulse")).toBeNull();
  expect(container.querySelectorAll("pre .token.number")).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Copied" })).toHaveLength(1);
});
