// Behavior lock for the local no-untranslated-ui-copy rule. Uses ESLint's
// RuleTester (flat config) driven by vitest.
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import rule from "./no-untranslated-ui-copy.mjs";

RuleTester.describe = describe;
RuleTester.it = it;

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

tester.run("no-untranslated-ui-copy", rule, {
  valid: [
    { code: '<div>{t("fleet.title")}</div>' },
    { code: '<Button aria-label={t("backToAgents")} />' },
    { code: '<Button title="—" />' },
    { code: '<span>24h · 7d</span>' },
    { code: "<button>↺</button>" },
    { code: "<span>⚠</span>" },
    { code: '<Button aria-label={label} />' },
    { code: '<Button placeholder={`${agentId}`} />' },
    { code: 'const labels = [t("back")]; const button = <Button aria-label={labels[0]} />;' },
    { code: 'const props = { "aria-label": t("back") }; const button = <Button {...props} />;' },
    { code: 'const runTimelineT = useTranslations("runTimeline"); const button = <Button aria-label={runTimelineT("zoom")} />;' },
    { code: '<span>{`${hours}h`}</span>' },
  ],
  invalid: [
    {
      code: "<div>Fleet</div>",
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: '<Button aria-label="Back to conversation" />',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: '<WindowSelect ariaLabel="Time window" />',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: '<Button placeholder={"Search agents"} />',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: '<Button title={formatWindow(24)} />',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: 'const label = "Back to conversation"; const button = <Button aria-label={label} />;',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: 'const labels = { back: "Back to conversation" }; const button = <Button aria-label={labels.back} />;',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: '<span>{notice.requireResponse ? "Decision" : "FYI"}</span>',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: '<Button aria-label={copyT()} />',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: 'const labels = ["Back to conversation"]; const button = <Button aria-label={labels[0]} />;',
      errors: [{ messageId: "untranslatedCopy" }],
    },
    {
      code: 'const props = { "aria-label": "Back to conversation" }; const button = <Button {...props} />;',
      errors: [{ messageId: "untranslatedCopy" }],
    },
  ],
});
