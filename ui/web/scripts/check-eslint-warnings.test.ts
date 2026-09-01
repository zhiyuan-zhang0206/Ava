import { describe, expect, it } from "vitest";

import { assertWarningBaseline } from "./check-eslint-warnings.mjs";

const PROJECT_ROOT = "/workspace/ui/web";
const baseline = [
  {
    file: "src/components/timeline/index.tsx",
    rule: "local/source-lines",
    line: 1,
  },
];

describe("assertWarningBaseline", () => {
  it("allows a warning with a matching file, rule, and line", () => {
    expect(
      assertWarningBaseline(
        [
          {
            filePath: `${PROJECT_ROOT}/src/components/timeline/index.tsx`,
            messages: [
              {
                severity: 1,
                ruleId: "local/source-lines",
                line: 1,
                message: "Source file exceeds the soft line budget.",
              },
            ],
          },
        ],
        baseline,
        PROJECT_ROOT,
      ),
    ).toEqual({ totalWarnings: 1, baselinedWarnings: 1 });
  });

  it("rejects an unbaselined warning identity", () => {
    expect(() =>
      assertWarningBaseline(
        [
          {
            filePath: `${PROJECT_ROOT}/src/components/timeline/index.tsx`,
            messages: [
              {
                severity: 1,
                ruleId: "@typescript-eslint/no-unnecessary-condition",
                line: 64,
                message: "Unexpected condition.",
              },
            ],
          },
        ],
        baseline,
        PROJECT_ROOT,
      ),
    ).toThrow("Unbaselined ESLint warnings");
  });

  it("rejects lint errors even when warnings match the baseline", () => {
    expect(() =>
      assertWarningBaseline(
        [
          {
            filePath: `${PROJECT_ROOT}/src/components/timeline/index.tsx`,
            messages: [
              {
                severity: 2,
                ruleId: "@typescript-eslint/no-explicit-any",
                line: 1,
                message: "Unexpected any.",
              },
            ],
          },
        ],
        baseline,
        PROJECT_ROOT,
      ),
    ).toThrow("ESLint errors");
  });
});
