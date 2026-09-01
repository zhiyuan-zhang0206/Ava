import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function checkFirstLoadJs(routeStats, route, budgetBytes) {
  const entry = routeStats.find((candidate) => candidate.route === route);
  if (entry === undefined) {
    throw new Error(`Next build diagnostics do not contain a first-load JavaScript entry for ${route}`);
  }

  const bytes = entry.firstLoadUncompressedJsBytes;
  if (!Number.isInteger(bytes) || bytes < 0) {
    throw new Error(`Next build diagnostics contain an invalid first-load JavaScript size for ${route}`);
  }

  if (bytes > budgetBytes) {
    throw new Error(
      `First-load JavaScript for ${route} is ${bytes} bytes, exceeding its ${budgetBytes} byte budget`,
    );
  }

  return bytes;
}

if (process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const route = "/";
  // Next 16.2.7 measured 1,207,268 bytes at introduction. This allows 42,732
  // bytes (3.5%) of intentional growth before a bundle-size review is required.
  const budgetBytes = 1_250_000;
  const diagnosticsPath = path.join(".next", "diagnostics", "route-bundle-stats.json");
  const routeStats = JSON.parse(readFileSync(diagnosticsPath, "utf8"));
  const bytes = checkFirstLoadJs(routeStats, route, budgetBytes);
  console.log(`First-load JavaScript for ${route}: ${bytes} bytes (budget: ${budgetBytes} bytes)`);
}
