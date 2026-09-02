// Behavior lock for the frontend's 500-line soft source-file budget.
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import rule from "./source-lines.mjs";

RuleTester.describe = describe;
RuleTester.it = it;

const tester = new RuleTester({
  languageOptions: { ecmaVersion: "latest", sourceType: "module" },
});

tester.run("source-lines", rule, {
  valid: [
    { code: `${"// line\n".repeat(499)}const value = 1;` },
  ],
  invalid: [
    {
      code: `${"// line\n".repeat(500)}const value = 1;`,
      errors: [{ messageId: "tooManyLines", data: { count: "501", max: "500" } }],
    },
  ],
});
