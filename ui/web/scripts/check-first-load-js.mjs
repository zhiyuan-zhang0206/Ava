import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * @typedef {{ route: string, firstLoadUncompressedJsBytes: number }} RouteBundleStat
 */

/**
 * @param {unknown} value
 * @returns {value is RouteBundleStat}
 */
function isRouteBundleStat(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    "route" in value &&
    typeof value.route === "string" &&
    "firstLoadUncompressedJsBytes" in value &&
    typeof value.firstLoadUncompressedJsBytes === "number"
  );
}

/**
 * Reject malformed Next diagnostics at the JSON boundary.
 *
 * @param {unknown} diagnostics
 * @returns {RouteBundleStat[]}
 */
export function parseRouteBundleStats(diagnostics) {
  if (!Array.isArray(diagnostics) || !diagnostics.every(isRouteBundleStat)) {
    throw new Error("Next build diagnostics do not contain valid route bundle statistics");
  }

  return diagnostics;
}

/**
 * @param {RouteBundleStat[]} routeStats
 * @param {string} route
 * @param {number} budgetBytes
 * @returns {number}
 */
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
  /** @type {unknown} */
  const diagnostics = JSON.parse(readFileSync(diagnosticsPath, "utf8"));
  const routeStats = parseRouteBundleStats(diagnostics);
  const bytes = checkFirstLoadJs(routeStats, route, budgetBytes);
  process.stdout.write(
    `First-load JavaScript for ${route}: ${bytes} bytes (budget: ${budgetBytes} bytes)\n`,
  );
}
