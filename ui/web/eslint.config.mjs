import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import nextPlugin from "@next/eslint-plugin-next";
import reactHooks from "eslint-plugin-react-hooks";

import sentenceCase from "./eslint-rules/sentence-case.mjs";
import layoutPrimitive from "./eslint-rules/layout-primitive.mjs";
import noUntranslatedUiCopy from "./eslint-rules/no-untranslated-ui-copy.mjs";
import sourceLines from "./eslint-rules/source-lines.mjs";

// Proper nouns / acronyms / brands allowed inside a Title-Case run (local/
// sentence-case). A run is permitted only when EVERY word is listed, so
// "GitHub API" passes while "API Key" is still flagged (→ "API key"). Keep this
// to genuine proper nouns — it is not an escape hatch for un-fixed copy. Seeded
// from a full sweep of src/ (see PR): the remaining Title-Case copy was fixed,
// only true proper nouns landed here.
const SENTENCE_CASE_ALLOW = [
  // Brands / products
  "Ava",
  "Grafana",
  "GitHub",
  "Claude",
  "Sonnet",
  "Opus",
  "Haiku",
  "MiMo",
  "DeepSeek",
  "Telegram",
  "Gmail",
  "Slack",
  "Chrome",
  "Safari",
  "Postgres",
  "Redis",
  "Pydantic",
  "TanStack",
  "React",
  "Next",
  // Acronyms / initialisms
  "API",
  "SDK",
  "URL",
  "ID",
  "CI",
  "PR",
  "WSL",
  "SSE",
  "JSON",
  "HTTP",
  "HTTPS",
  "CLI",
  "MCP",
  "OKF",
  "LLM",
  "TPS",
  "OK",
  "DB",
  "DOM",
  "UI",
  "UX",
  "SVG",
  "CSS",
  "HTML",
  "CORS",
  "LAN",
  "IME",
  "TZ",
  "P0",
  "P50",
  "P90",
  "SIGKILL",
  "SIGTERM",
  "PID",
  "CPU",
  "RAM",
  "TTL",
  "RSS",
  "GET",
  "POST",
  "PUT",
  // English pronoun — always capitalized, never Title Case
  "I",
];

export default tseslint.config(
  // ── Global ignore ──
  {
    ignores: [
      ".next/**",
      ".builds/**",
      "node_modules/**",
      "src/lib/types-generated.ts",
      "*.config.*",
      // Local ESLint plugin source — plain .mjs outside the TS project, so the
      // type-aware base configs must not try to lint it.
      "eslint-rules/**",
    ],
  },

  // ── Base: ESLint recommended + TypeScript strict type-aware ──
  eslint.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,

  // TypeScript parser config (required for type-aware rules)
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },

  // ── Next.js ──
  {
    plugins: { "@next/next": nextPlugin },
    rules: {
      ...nextPlugin.configs.recommended.rules,
      ...nextPlugin.configs["core-web-vitals"].rules,
      // App Router projects have no pages/ directory
      "@next/next/no-html-link-for-pages": "off",
    },
  },

  // ── React Hooks ──
  {
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs["recommended-latest"].rules,
      // Sync setState inside useEffect is legitimate in some scenarios (e.g. scroll token)
      "react-hooks/set-state-in-effect": "warn",
    },
  },

  // ── Custom tightening ──
  {
    rules: {
      // ── any: zero tolerance ──
      "@typescript-eslint/no-explicit-any": "error",

      // ── Template string type safety ──
      "@typescript-eslint/restrict-template-expressions": ["error", {
        allowAny: false,
        allowBoolean: true,
        allowNever: false,
        allowNullish: true,
        allowNumber: true,
        allowRegExp: false,
      }],

      // ── console residue check (allow warn/error for error-boundary logging) ──
      "no-console": ["warn", { allow: ["warn", "error"] }],

      // ── enforce import type ──
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { prefer: "type-imports", fixStyle: "separate-type-imports" },
      ],
      "@typescript-eslint/consistent-type-exports": [
        "error",
        { fixMixedExportsWithInlineTypeSpecifier: true },
      ],

      // ── Array type syntax: enforce T[] style ──
      "@typescript-eslint/array-type": ["error", { default: "array" }],

      // ── unnecessary condition downgraded to warn (has FPs) ──
      "@typescript-eslint/no-unnecessary-condition": "warn",

      // ── Type definitions: allow both type and interface to coexist ──
      "@typescript-eslint/consistent-type-definitions": "error",

      // ── Unused variables: allow _ prefix to ignore ──
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],

      // ── non-null assertion downgraded to warn ──
      "@typescript-eslint/no-non-null-assertion": "warn",

      // ── Promise misuse: JSX event handlers allow async wrapped in void ──
      "@typescript-eslint/no-misused-promises": [
        "error",
        { checksVoidReturn: { attributes: false } },
      ],

      // ── no-confusing-void-expression: off ──
      // This project uses `void fn()` as an explicit "fire-and-forget" marker,
      // signaling intentional return-value disposal (similar to Go's `go func()`).
      // It's a deliberate design pattern, not a bug.
      "@typescript-eslint/no-confusing-void-expression": "off",

      // ── unbound-method downgraded to warn ──
      // React Query queryFn: api.getConfig / TanStack Table etc. are ecosystem
      // standard idioms; methods reference dependencies via closure rather than
      // this, not a bug. Kept as warn for review visibility.
      "@typescript-eslint/unbound-method": "warn",

      // ── unnecessary type assertion downgraded to warn ──
      "@typescript-eslint/no-unnecessary-type-assertion": "warn",

      // ── unsafe assignment downgraded to warn ──
      "@typescript-eslint/no-unsafe-assignment": "error",

      // ── React 19 doesn't need import React ──
      "react/react-in-jsx-scope": "off",

      // ── Existing tightening preserved ──
      "prefer-const": "error",
      "no-var": "error",
      "eqeqeq": ["error", "always", { null: "ignore" }],

      // ── Forbid unused expressions (guards against missed void calls) ──
      "@typescript-eslint/no-unused-expressions": [
        "error",
        { allowShortCircuit: true, allowTernary: true },
      ],

      // ── Forbid empty functions ──
      "@typescript-eslint/no-empty-function": "error",
      "@typescript-eslint/no-deprecated": "error",
      "@typescript-eslint/no-dynamic-delete": "error",
      "@typescript-eslint/no-meaningless-void-operator": "error",
    },
  },

  // ── English-only UI copy (AGENTS.md "English only — no raw CJK") ──
  // No lint previously covered this: the only CJK-forbidding lint in the repo
  // (scripts/lint_agent_docstrings.py) is Python-only (`types: [python]`),
  // scoped to ava/*.py + plugins/*/*.py agent-visible SDK docstrings — it
  // structurally cannot reach .tsx. This mirrors its CJK ranges (Unified
  // Ideographs + CJK punctuation + fullwidth ASCII) for JSX text and the
  // common copy-carrying attributes. Test files are exempt — fixtures that
  // deliberately exercise non-English input (composer/api payloads) stay put.
  {
    files: ["src/**/*.tsx"],
    ignores: ["**/*.test.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXText[value=/[\\u4e00-\\u9fff\\u3000-\\u303f\\uff00-\\uffef]/]",
          message:
            "UI copy must be English (AGENTS.md \"English only\") — no raw CJK in JSX text.",
        },
        {
          selector:
            "JSXAttribute[name.name=/^(label|title|description|placeholder|aria-label)$/] > Literal[value=/[\\u4e00-\\u9fff\\u3000-\\u303f\\uff00-\\uffef]/]",
          message:
            "UI copy must be English (AGENTS.md \"English only\") — no raw CJK in label/title/description/placeholder/aria-label.",
        },
      ],
    },
  },

  // ── next-intl migration gate ──
  // These surfaces have completed their translation sweep. Keep the gate
  // deliberately scoped until the remaining routes migrate; applying it to all
  // src/ now would turn this focused change into an unrelated UI-copy rewrite.
  {
    files: [
      "src/components/fleet/**/*.{ts,tsx}",
      "src/app/insights/**/*.{ts,tsx}",
      "src/app/memory/graph/**/*.{ts,tsx}",
      "src/components/spawn-button.tsx",
    ],
    ignores: ["**/*.test.{ts,tsx}"],
    rules: {
      "local/no-untranslated-ui-copy": [
        "error",
        { allow: ["$AVA_HOME/logs/rollout-<epoch>.log"] },
      ],
    },
  },

  // Custom local rule (eslint-rules/layout-primitive.mjs): the six layout-contract
  // classes (flex/flex-1/flex-col/min-w-0/min-h-0/overflow-hidden) must come from
  // @/lib/layout as constants — the jsdom class-contract layer of invariants I1–I6
  // asserts these on key containers (Task #1024, R4). Hand-written variants bypass
  // the primitive and the contract layer silently stops guarding them.
  // ── Sentence case: forbid Title Case in user-facing copy ──
  // Custom local rule (eslint-rules/sentence-case.mjs). Fires on JSX text and
  // copy-carrying attributes/props (title/label/aria-label/placeholder/…) when
  // it finds two+ consecutive Capitalized words that aren't all whitelisted
  // proper nouns. Test files exempt — fixtures deliberately carry odd casing.
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}"],
    plugins: {
      local: {
        rules: {
          "sentence-case": sentenceCase,
          "layout-primitive": layoutPrimitive,
          "no-untranslated-ui-copy": noUntranslatedUiCopy,
          "source-lines": sourceLines,
        },
      },
    },
    rules: {
      "local/sentence-case": ["error", { allow: SENTENCE_CASE_ALLOW }],
      "local/layout-primitive": "error",
    },
  },

  // ── Per-file 500-line soft budget ──
  // This measures physical lines so the warning reflects the total file a
  // maintainer must navigate. It deliberately remains a warning: the existing
  // outliers stay visible without turning an unrelated edit into a component
  // split. The lint warning baseline preserves existing identities while
  // rejecting new or duplicate diagnostics, without coupling the gate to a
  // merge-reference-wide warning count.
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}", "src/lib/types-generated.ts"],
    rules: {
      "local/source-lines": ["warn", { max: 500 }],
    },
  },

  // ── Per-file line budget (outlier cleanup, user ruling 2026-08-07) ──
  // Mirror of the Python 500-soft / 800-hard discipline (AGENTS.md). The local
  // source-lines rule above makes the 500 tier visible as a warning; this core
  // rule retains the 800-line hard ceiling. Blank/comment lines are skipped at
  // the hard tier so a comment-dense file is not penalized. Test files are
  // exempt (fixtures carry volume; a 1200 cap lands with the test-outlier
  // sweep).
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}", "src/lib/types-generated.ts"],
    rules: {
      "max-lines": ["error", { max: 800, skipBlankLines: true, skipComments: true }],
    },
  },

  // ── Test files ──
  {
    files: ["**/*.test.{ts,tsx}"],
    rules: {
      // `.closest("button")!` / `querySelector(...)!` are idiomatic in
      // testing-library code — a wrong selector fails the test loudly on the
      // next line anyway, so the assertion adds no risk, only noise.
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
);
