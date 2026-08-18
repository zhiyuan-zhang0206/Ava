// Local ESLint rule: forbid Title Case in user-facing copy.
//
// Why: the frontend has a firm sentence-case convention (AGENTS.md; PRs #757,
// #771). Title Case kept creeping back after manual sweeps, so this rule locks
// it down.
//
// Detection signature — deliberately narrow to keep false positives low:
// two OR MORE *consecutive* words that each begin with an uppercase letter,
// separated only by spaces/tabs ("System Note", "Show Terminated"). A lone
// capitalized word (sentence start: "Back to agents", "Live cluster health")
// never trips the rule — only a run of them does. That is the classic Title
// Case fingerprint; "any non-first word capitalized" was rejected as too noisy.
//
// Whitelist (proper nouns / acronyms / brands, passed via the `allow` option):
// a word passes when it is whitelisted OR when it is the first word of the
// whole string (a capitalized first word is correct sentence case — "Agent
// SDK", "Enable API"). A run is a violation when any *checked* word is neither.
// So "GitHub API" passes (both listed), "Agent SDK" passes ("Agent" is the
// first word, "SDK" is listed), but "API Key" is still flagged — "Key" is not a
// proper noun and not the first word, so the intended copy is "API key". The
// allow list lives in eslint.config.mjs so it stays maintainable and is never a
// blanket escape. Known non-goals: capitalized words joined by "&", "/" or ":"
// ("Security & Secrets") are single words per side, not a whitespace run, so the
// rule does not catch them — the sweep fixed those by hand.
//
// Scope — only positions a user actually reads:
//   • JSXText (text between tags)
//   • JSX attributes that carry copy: title, label, aria-label, placeholder,
//     tooltip, alt, description, heading, badge, subtitle, caption
//   • object-literal properties whose KEY is one of those copy names (label
//     maps / section configs: `{ label: "Agent Memory" }`)
// Comments, import paths, className, enum/id values, and identifiers are never
// inspected. Test files are exempt (fixtures deliberately carry odd casing).

// Attribute / property keys whose string value is user-facing copy.
const COPY_KEYS = new Set([
  "title",
  "label",
  "aria-label",
  "placeholder",
  "tooltip",
  "alt",
  "description",
  "heading",
  "badge",
  "subtitle",
  "caption",
  "cta",
]);

const WORD = "[A-Z][A-Za-z0-9]*";
// Two or more capitalized words separated only by spaces/tabs (never newlines,
// so a run cannot bleed across a sentence/line break).
const RUN_RE = new RegExp(`\\b${WORD}(?:[ \\t]+${WORD})+`, "g");
const SPLIT_RE = /[ \t]+/;

function findViolations(text, allow) {
  const out = [];
  let m;
  RUN_RE.lastIndex = 0;
  while ((m = RUN_RE.exec(text))) {
    const run = m[0];
    const words = run.split(SPLIT_RE);
    // The run's first word is exempt only when the run sits at the very start of
    // the string (leading whitespace aside) — a capitalized first word is
    // correct sentence case. Every other word must be a whitelisted proper noun.
    const startExempt = text.slice(0, m.index).trim() === "";
    const bad = words.some((w, i) => !(allow.has(w) || (i === 0 && startExempt)));
    if (bad) out.push(run);
  }
  return out;
}

// Extract a plain string from a Literal / no-expression TemplateLiteral, else null.
function staticString(node) {
  if (!node) return null;
  if (node.type === "Literal" && typeof node.value === "string") return node.value;
  if (node.type === "TemplateLiteral" && node.expressions.length === 0) {
    return node.quasis.map((q) => q.value.cooked ?? q.value.raw).join("");
  }
  if (node.type === "JSXExpressionContainer") return staticString(node.expression);
  return null;
}

function attrName(node) {
  const n = node.name;
  if (n.type === "JSXIdentifier") return n.name;
  if (n.type === "JSXNamespacedName") return `${n.namespace.name}:${n.name.name}`;
  return null;
}

function keyName(node) {
  if (node.computed) return null;
  const k = node.key;
  if (k.type === "Identifier") return k.name;
  if (k.type === "Literal" && typeof k.value === "string") return k.value;
  return null;
}

/** @type {import("eslint").Rule.RuleModule} */
const rule = {
  meta: {
    type: "problem",
    docs: { description: "Enforce sentence case in user-facing copy (forbid Title Case runs)." },
    schema: [
      {
        type: "object",
        properties: {
          allow: { type: "array", items: { type: "string" } },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      titleCase:
        'Title Case in user-facing copy: "{{run}}". Use sentence case (capitalize only the first word). If these are proper nouns/acronyms, add them to the `allow` list of local/sentence-case in eslint.config.mjs.',
    },
  },
  create(context) {
    const allow = new Set(context.options[0]?.allow ?? []);

    function report(node, text) {
      for (const run of findViolations(text, allow)) {
        context.report({ node, messageId: "titleCase", data: { run } });
      }
    }

    return {
      JSXText(node) {
        report(node, node.value);
      },
      JSXAttribute(node) {
        const name = attrName(node);
        if (!name || !COPY_KEYS.has(name)) return;
        const value = staticString(node.value);
        if (value != null) report(node.value, value);
      },
      Property(node) {
        const key = keyName(node);
        if (!key || !COPY_KEYS.has(key)) return;
        const value = staticString(node.value);
        if (value != null) report(node.value, value);
      },
    };
  },
};

export default rule;
