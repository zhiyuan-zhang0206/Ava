// Local ESLint rule: forbid hand-writing the layout-contract classes.
//
// Why: the frontend's layout invariants (I1–I6, lib/layout.ts) are guarded
// by a two-layer defense — jsdom class-contract tests assert that key
// containers carry these exact classes, and Playwright measures the real
// layout at 320/390/768. Each of these classes has broken mobile layout as a
// real P0 (#874 vertical: missing display:flex; #979 horizontal: missing
// min-w-0). A class written by hand in a className string bypasses the
// primitive (lib/layout.ts) and the jsdom contract layer silently stops
// guarding it — a layout redesign that changes a primitive's value would
// miss that spot. So these six classes are only ever referenced through the
// exported constants.
//
// Detection: a forbidden class token inside a STATIC className string — a
// JSX className attribute (string or expression-less template), or a string
// argument of cn(...). Token matching is whitespace-delimited, so compound
// classes never false-positive: "inline-flex", "flex-1", "flex-col",
// "min-w-[220px]", "data-[x]:flex-col" are all single tokens, not "flex".
// Dynamic classNames (variables) cannot be checked and are out of scope —
// they must compose the primitives themselves.

// The contract classes → the lib/layout.ts constant that replaces them.
// Keep in sync with ui/web/src/lib/layout.ts.
const PRIMITIVES = {
  flex: "FLEX",
  "flex-1": "FLEX_1",
  "flex-col": "FLEX_COL",
  "min-w-0": "MIN_W_0",
  "min-h-0": "MIN_H_0",
  "overflow-hidden": "OVERFLOW_HIDDEN",
};

// Extract a plain string from a Literal / no-expression TemplateLiteral, else null.
function staticString(node) {
  if (!node) return null;
  if (node.type === "Literal" && typeof node.value === "string") return node.value;
  if (node.type === "TemplateLiteral" && node.expressions.length === 0) {
    return node.quasis.map((q) => q.value.cooked ?? q.value.raw).join("");
  }
  return null;
}

function findViolations(text) {
  const out = [];
  for (const token of text.split(/\s+/)) {
    if (token in PRIMITIVES) out.push(token);
  }
  return out;
}

function attrName(node) {
  const n = node.name;
  if (n.type === "JSXIdentifier") return n.name;
  return null;
}

/** @type {import("eslint").Rule.RuleModule} */
const rule = {
  meta: {
    type: "problem",
    docs: {
      description:
        "Forbid hand-writing layout-contract classes (flex/flex-1/flex-col/min-w-0/min-h-0/overflow-hidden) — import them from @/lib/layout.",
    },
    schema: [],
    messages: {
      rawPrimitive:
        'Layout contract class "{{cls}}" must come from @/lib/layout as {{constant}} — never written by hand (the jsdom contract layer of invariants I1–I6 asserts these classes on key containers; a hand-written variant bypasses it and a primitive change silently misses it).',
    },
  },
  create(context) {
    function report(node, text) {
      for (const cls of findViolations(text)) {
        context.report({
          node,
          messageId: "rawPrimitive",
          data: { cls, constant: PRIMITIVES[cls] },
        });
      }
    }

    return {
      JSXAttribute(node) {
        if (attrName(node) !== "className") return;
        const value = staticString(node.value);
        if (value != null) report(node.value, value);
      },
      CallExpression(node) {
        const callee = node.callee;
        const isCn =
          callee.type === "Identifier" && callee.name === "cn";
        if (!isCn) return;
        for (const arg of node.arguments) {
          const value = staticString(arg);
          if (value != null) report(arg, value);
        }
      },
    };
  },
};

export default rule;
