// Behavior lock for the local layout-primitive rule. Uses ESLint's
// RuleTester (flat config) driven by vitest's describe/it.
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import rule from "./layout-primitive.mjs";

RuleTester.describe = describe;
RuleTester.it = it;

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

tester.run("layout-primitive", rule, {
  valid: [
    // Primitive-free classNames are untouched.
    { code: '<div className="items-center gap-2">x</div>' },
    // Compound classes are single tokens — never false positives.
    { code: '<div className="inline-flex items-center">x</div>' },
    { code: '<div className="flex-auto min-w-[220px]">x</div>' },
    { code: '<div className="flex-col-reverse">x</div>' },
    { code: '<div className="data-[orientation=horizontal]:flex-col">x</div>' },
    { code: '<div className="min-w-[80px]">x</div>' },
    // Dynamic classNames are out of scope (must compose primitives).
    { code: "const c = someVar; <div className={c}>x</div>" },
    // Template with expressions — out of scope.
    { code: '<div className={`p-2 ${x ? "flex" : "block"}`}>x</div>' },
    // cn() with no forbidden tokens.
    { code: 'cn("items-center", className)' },
    // Non-className attributes are never inspected.
    { code: '<div aria-label="flex">x</div>' },
    { code: '<div data-flex="min-w-0">x</div>' },
    // Comments and identifiers.
    { code: "// min-w-0 flex flex-col\nconst x = 1;" },
  ],
  invalid: [
    // Each primitive in a plain className string.
    { code: '<div className="flex">x</div>', errors: [{ messageId: "rawPrimitive" }] },
    { code: '<div className="flex-1">x</div>', errors: [{ messageId: "rawPrimitive" }] },
    { code: '<div className="flex-col">x</div>', errors: [{ messageId: "rawPrimitive" }] },
    { code: '<div className="min-w-0">x</div>', errors: [{ messageId: "rawPrimitive" }] },
    { code: '<div className="min-h-0">x</div>', errors: [{ messageId: "rawPrimitive" }] },
    { code: '<div className="overflow-hidden">x</div>', errors: [{ messageId: "rawPrimitive" }] },
    // Mixed with ordinary classes — one report per forbidden token.
    {
      code: '<div className="relative flex min-h-0 flex-1">x</div>',
      errors: [
        { messageId: "rawPrimitive" },
        { messageId: "rawPrimitive" },
        { messageId: "rawPrimitive" },
      ],
    },
    // Inside cn() string arguments.
    { code: 'cn("relative", "flex")', errors: [{ messageId: "rawPrimitive" }] },
    { code: 'cn("flex min-w-0", className)', errors: [{ messageId: "rawPrimitive" }, { messageId: "rawPrimitive" }] },
    // Single-quoted strings.
    { code: "<div className='flex'>x</div>", errors: [{ messageId: "rawPrimitive" }] },
  ],
});
