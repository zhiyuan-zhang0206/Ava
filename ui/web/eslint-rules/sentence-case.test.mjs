// Behavior lock for the local sentence-case rule. Uses ESLint's RuleTester
// (flat config) driven by vitest's describe/it.
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import rule from "./sentence-case.mjs";

RuleTester.describe = describe;
RuleTester.it = it;

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

// A small self-contained allow list — the real config seeds a bigger one.
const opts = [{ allow: ["GitHub", "API", "SDK", "Telegram"] }];

tester.run("sentence-case", rule, {
  valid: [
    // Sentence case: a lone capitalized first word never trips the rule.
    { code: "<div>Back to agents</div>", options: opts },
    { code: "<div>Live cluster health</div>", options: opts },
    // A run whose non-first words are all whitelisted proper nouns.
    { code: "<div>GitHub API docs</div>", options: opts },
    // First-word-of-string exemption: capitalized first word + acronym is fine.
    { code: '<Foo label="Agent SDK" />', options: opts },
    { code: '<Foo label="Enable API" />', options: opts },
    { code: '<Foo title="Web & Telegram" />', options: opts },
    // Object property with a whitelisted run.
    { code: 'const x = { label: "GitHub API" };', options: opts },
    // Non-copy positions are never inspected.
    { code: '<Foo className="Grid Layout" />', options: opts },
    { code: 'const x = { name: "System Note", id: "Some Value" };', options: opts },
    { code: 'import { Foo } from "./Some Path";', options: opts },
  ],
  invalid: [
    // Classic Title Case in JSX text.
    {
      code: "<div>System Note</div>",
      options: opts,
      errors: [{ messageId: "titleCase" }],
    },
    {
      code: "<div>Show Terminated</div>",
      options: opts,
      errors: [{ messageId: "titleCase" }],
    },
    // "API Key" — API is exempt (first word), but Key is neither whitelisted
    // nor first, so the run is flagged (intended copy: "API key").
    {
      code: '<Foo label="API Key" />',
      options: opts,
      errors: [{ messageId: "titleCase" }],
    },
    // A run not at the string start gets no first-word exemption.
    {
      code: '<Foo title="Enable the System Note" />',
      options: opts,
      errors: [{ messageId: "titleCase" }],
    },
    // Copy-carrying object property.
    {
      code: 'const x = { label: "Data Plane" };',
      options: opts,
      errors: [{ messageId: "titleCase" }],
    },
    // aria-label attribute.
    {
      code: '<Foo aria-label="Show Terminated" />',
      options: opts,
      errors: [{ messageId: "titleCase" }],
    },
  ],
});
