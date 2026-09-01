import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * @typedef {{ severity: number, ruleId: string | null, line: number, message: string }} LintMessage
 * @typedef {{ filePath: string, messages: LintMessage[] }} LintResult
 * @typedef {{ file: string, rule: string, line: number }} WarningIdentity
 * @typedef {{ warnings: WarningIdentity[] }} WarningBaseline
 */

/**
 * @param {unknown} value
 * @returns {value is LintMessage}
 */
function isLintMessage(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    "severity" in value &&
    typeof value.severity === "number" &&
    "ruleId" in value &&
    (typeof value.ruleId === "string" || value.ruleId === null) &&
    "line" in value &&
    typeof value.line === "number" &&
    "message" in value &&
    typeof value.message === "string"
  );
}

/**
 * @param {unknown} value
 * @returns {value is LintResult}
 */
function isLintResult(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    "filePath" in value &&
    typeof value.filePath === "string" &&
    "messages" in value &&
    Array.isArray(value.messages) &&
    value.messages.every(isLintMessage)
  );
}

/**
 * @param {unknown} value
 * @returns {value is WarningIdentity}
 */
function isWarningIdentity(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    "file" in value &&
    typeof value.file === "string" &&
    "rule" in value &&
    typeof value.rule === "string" &&
    "line" in value &&
    Number.isInteger(value.line) &&
    value.line > 0
  );
}

/**
 * @param {unknown} report
 * @returns {LintResult[]}
 */
function parseEslintReport(report) {
  if (!Array.isArray(report) || !report.every(isLintResult)) {
    throw new Error("ESLint did not produce a valid JSON report.");
  }

  return report;
}

/**
 * @param {unknown} baseline
 * @returns {WarningBaseline}
 */
function parseWarningBaseline(baseline) {
  if (
    typeof baseline !== "object" ||
    baseline === null ||
    !("warnings" in baseline) ||
    !Array.isArray(baseline.warnings) ||
    !baseline.warnings.every(isWarningIdentity)
  ) {
    throw new Error("ESLint warning baseline is invalid.");
  }

  return baseline;
}

/**
 * @param {WarningIdentity} warning
 * @returns {string}
 */
function warningKey(warning) {
  return `${warning.file}:${warning.rule}:${warning.line}`;
}

/**
 * @param {string} filePath
 * @param {string} projectRoot
 * @returns {string}
 */
function projectPath(filePath, projectRoot) {
  const relativePath = path.relative(projectRoot, filePath);
  if (
    relativePath === "" ||
    relativePath === ".." ||
    relativePath.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relativePath)
  ) {
    return filePath;
  }

  return relativePath;
}

/**
 * Fail for any ESLint error or warning identity absent from the committed baseline.
 *
 * @param {unknown} report
 * @param {unknown} warningBaseline
 * @param {string} projectRoot
 * @returns {{ totalWarnings: number, baselinedWarnings: number }}
 */
export function assertWarningBaseline(report, warningBaseline, projectRoot) {
  const results = parseEslintReport(report);
  if (!Array.isArray(warningBaseline) || !warningBaseline.every(isWarningIdentity)) {
    throw new Error("ESLint warning baseline is invalid.");
  }

  /** @type {Map<string, number>} */
  const remainingBaseline = new Map();
  for (const warning of warningBaseline) {
    const key = warningKey(warning);
    remainingBaseline.set(key, (remainingBaseline.get(key) ?? 0) + 1);
  }

  /** @type {string[]} */
  const lintErrors = [];
  /** @type {string[]} */
  const unexpectedWarnings = [];
  let totalWarnings = 0;
  let baselinedWarnings = 0;

  for (const result of results) {
    const file = projectPath(result.filePath, projectRoot);
    for (const message of result.messages) {
      if (message.severity === 2) {
        lintErrors.push(`${file}:${message.line} ${message.message}`);
        continue;
      }
      if (message.severity !== 1) {
        continue;
      }

      totalWarnings += 1;
      if (message.ruleId === null || !Number.isInteger(message.line) || message.line < 1) {
        unexpectedWarnings.push(`${file}:${message.line} ${message.message}`);
        continue;
      }

      const warning = { file, rule: message.ruleId, line: message.line };
      const key = warningKey(warning);
      const remaining = remainingBaseline.get(key) ?? 0;
      if (remaining === 0) {
        unexpectedWarnings.push(`${file}:${message.ruleId}:${message.line}`);
        continue;
      }

      remainingBaseline.set(key, remaining - 1);
      baselinedWarnings += 1;
    }
  }

  if (lintErrors.length > 0) {
    throw new Error(`ESLint errors:\n${lintErrors.join("\n")}`);
  }
  if (unexpectedWarnings.length > 0) {
    throw new Error(`Unbaselined ESLint warnings:\n${unexpectedWarnings.join("\n")}`);
  }

  return { totalWarnings, baselinedWarnings };
}

function main() {
  /** @type {unknown} */
  const report = JSON.parse(readFileSync(0, "utf8"));
  /** @type {unknown} */
  const baseline = JSON.parse(
    readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "eslint-warning-baseline.json"), "utf8"),
  );
  const warningBaseline = parseWarningBaseline(baseline);
  const result = assertWarningBaseline(report, warningBaseline.warnings, process.cwd());
  process.stdout.write(
    `ESLint warnings match baseline: ${result.baselinedWarnings}/${result.totalWarnings}.\n`,
  );
}

const entrypoint = process.argv.at(1);
if (entrypoint !== undefined && path.resolve(entrypoint) === fileURLToPath(import.meta.url)) {
  main();
}
