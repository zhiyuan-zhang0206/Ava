// Local ESLint rule: keep frontend source files within the 500-line soft budget.
//
// Physical lines are deliberate: a long source file is hard to navigate even
// when some of that volume is comments or whitespace. The rule is configured
// as a warning, so it stays visible without forcing opportunistic component
// splits in unrelated changes.

/** @type {import("eslint").Rule.RuleModule} */
const rule = {
  meta: {
    type: "suggestion",
    docs: {
      description: "Warn when a frontend source file exceeds the soft line budget.",
    },
    schema: [
      {
        type: "object",
        properties: { max: { type: "integer", minimum: 1 } },
        additionalProperties: false,
      },
    ],
    messages: {
      tooManyLines:
        "Source file has {{count}} lines (soft limit: {{max}}). Keep new source files below the budget; split this file only as part of a focused refactor.",
    },
  },
  create(context) {
    const max = context.options[0]?.max ?? 500;

    return {
      Program(node) {
        const count = context.sourceCode.lines.length;
        if (count > max) {
          context.report({
            node,
            messageId: "tooManyLines",
            data: { count, max },
          });
        }
      },
    };
  },
};

export default rule;
