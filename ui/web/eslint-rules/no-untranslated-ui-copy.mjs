// Local ESLint rule: require translated copy in the UI surfaces that have
// completed the next-intl migration. The rule is deliberately scope-agnostic;
// eslint.config.mjs chooses the migrated files it protects.

const COPY_ATTRIBUTES = new Set(["label", "title", "placeholder", "aria-label", "ariaLabel"]);

// Values made only of punctuation or numeric time/quantity tokens are not
// language copy. This admits values such as "24h · 7d" while excluding words.
const NON_COPY_TEXT = /^(?:(?:\d+(?:[hHdDmMsS])?)|[\s.,:;!?%+−–—/\\|()[\]{}<>*=·×#$↺⚠])+$/;

function attributeName(node) {
  return node.name.type === "JSXIdentifier" ? node.name.name : null;
}

function isNonCopyText(value, allow) {
  const trimmed = value.trim();
  return trimmed === "" || allow.has(trimmed) || NON_COPY_TEXT.test(value);
}

function isNonCopyTemplate(node, allow) {
  return isNonCopyText(
    node.quasis.map((quasi) => quasi.value.cooked ?? quasi.value.raw).join("0"),
    allow,
  );
}

function isAllowedExpression(
  node,
  allow,
  isAllowedIdentifier,
  isAllowedMemberExpression,
  isKnownTranslationCall,
) {
  switch (node.type) {
    case "CallExpression":
      return isKnownTranslationCall(node);
    case "Identifier":
      return isAllowedIdentifier(node);
    case "MemberExpression":
      return isAllowedMemberExpression(node);
    case "Literal":
      return typeof node.value !== "string" || isNonCopyText(node.value, allow);
    case "TemplateLiteral":
      return isNonCopyTemplate(node, allow)
        && node.expressions.every((expression) =>
          isAllowedExpression(
            expression,
            allow,
            isAllowedIdentifier,
            isAllowedMemberExpression,
            isKnownTranslationCall,
          ),
        );
    case "ConditionalExpression":
      return isAllowedExpression(
        node.consequent,
        allow,
        isAllowedIdentifier,
        isAllowedMemberExpression,
        isKnownTranslationCall,
      ) && isAllowedExpression(
        node.alternate,
        allow,
        isAllowedIdentifier,
        isAllowedMemberExpression,
        isKnownTranslationCall,
      );
    case "LogicalExpression":
      return isAllowedExpression(
        node.left,
        allow,
        isAllowedIdentifier,
        isAllowedMemberExpression,
        isKnownTranslationCall,
      ) && isAllowedExpression(
        node.right,
        allow,
        isAllowedIdentifier,
        isAllowedMemberExpression,
        isKnownTranslationCall,
      );
    case "TSAsExpression":
    case "TSTypeAssertion":
    case "TypeCastExpression":
      return isAllowedExpression(
        node.expression,
        allow,
        isAllowedIdentifier,
        isAllowedMemberExpression,
        isKnownTranslationCall,
      );
    default:
      return false;
  }
}

/** @type {import("eslint").Rule.RuleModule} */
const rule = {
  meta: {
    type: "problem",
    docs: {
      description: "Require next-intl translations for user-facing JSX copy.",
    },
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
      untranslatedCopy:
        "User-facing UI copy must come from t(). Use a narrowly scoped allowlist only for non-language values.",
    },
  },
  create(context) {
    const allow = new Set(context.options[0]?.allow ?? []);
    const resolvingDefinitions = new Set();

    function localVariable(node) {
      let scope = context.sourceCode.getScope(node);
      while (scope) {
        const variable = scope.set.get(node.name);
        if (variable) return variable;
        scope = scope.upper;
      }
      return null;
    }

    function isUseTranslationsImport(node) {
      return localVariable(node)?.defs.some((definition) =>
        definition.type === "ImportBinding"
        && definition.node.type === "ImportSpecifier"
        && definition.node.imported.type === "Identifier"
        && definition.node.imported.name === "useTranslations"
        && definition.parent?.source.value === "next-intl",
      ) ?? false;
    }

    function isKnownTranslationCall(node) {
      if (node.type !== "CallExpression" || node.callee.type !== "Identifier") return false;

      const initializer = localVariable(node.callee)?.defs.find(
        (def) => def.type === "Variable",
      )?.node.init;
      return initializer?.type === "CallExpression"
        && initializer.callee.type === "Identifier"
        && isUseTranslationsImport(initializer.callee);
    }

    function isAllowedIdentifier(node) {
      const variable = localVariable(node);
      const definition = variable?.defs.find((def) => def.type === "Variable");
      const initializer = definition?.node.init;

      // Parameters, imports, globals, and uninitialized variables are caller
      // placeholders. A local initializer must itself be a permitted value.
      if (initializer == null) return true;
      if (resolvingDefinitions.has(definition)) return false;

      resolvingDefinitions.add(definition);
      try {
        return isAllowedExpression(
          initializer,
          allow,
          isAllowedIdentifier,
          isAllowedMemberExpression,
          isKnownTranslationCall,
        );
      } finally {
        resolvingDefinitions.delete(definition);
      }
    }

    function memberPropertyName(node) {
      if (!node.computed && node.property.type === "Identifier") return node.property.name;
      if (node.computed) return staticStringValue(node.property);
      return null;
    }

    function objectPropertyName(node) {
      if (!node.computed && node.key.type === "Identifier") return node.key.name;
      if (node.key.type === "Literal" && typeof node.key.value === "string") {
        return node.key.value;
      }
      return null;
    }

    function staticStringValue(node) {
      if (node.type === "Literal" && typeof node.value === "string") return node.value;
      if (node.type !== "Identifier") return null;

      const initializer = localVariable(node)?.defs.find(
        (definition) => definition.type === "Variable",
      )?.node.init;
      return initializer?.type === "Literal" && typeof initializer.value === "string"
        ? initializer.value
        : null;
    }

    function isAllowedMemberExpression(node) {
      const staticValue = staticMemberValue(node);
      return staticValue == null || isAllowedExpression(
        staticValue,
        allow,
        isAllowedIdentifier,
        isAllowedMemberExpression,
        isKnownTranslationCall,
      );
    }

    function staticMemberValue(node) {
      const object = node.object.type === "Identifier"
        ? localVariable(node.object)?.defs.find((def) => def.type === "Variable")?.node.init
        : node.object.type === "MemberExpression"
          ? staticMemberValue(node.object)
          : null;

      if (object?.type === "ObjectExpression") {
        const propertyName = memberPropertyName(node);
        if (propertyName == null) return null;
        const property = object.properties.find(
          (candidate) => candidate.type === "Property" && objectPropertyName(candidate) === propertyName,
        );
        return property?.type === "Property" ? property.value : null;
      }

      if (object?.type === "ArrayExpression") {
        const index = node.computed && node.property.type === "Literal" && typeof node.property.value === "number"
          ? node.property.value
          : null;
        return index != null && Number.isInteger(index) && index >= 0
          ? object.elements[index] ?? null
          : null;
      }

      return null;
    }

    function staticArrayExpression(node) {
      if (node.type === "ArrayExpression") return node;
      if (node.type === "Identifier") {
        const initializer = localVariable(node)?.defs.find(
          (definition) => definition.type === "Variable",
        )?.node.init;
        return initializer?.type === "ArrayExpression" ? initializer : null;
      }
      if (node.type === "MemberExpression") {
        const value = staticMemberValue(node);
        return value?.type === "ArrayExpression" ? value : null;
      }
      return null;
    }

    function staticJoinElements(node) {
      if (node.callee.type !== "MemberExpression" || memberPropertyName(node.callee) !== "join") {
        return null;
      }
      return staticArrayExpression(node.callee.object)?.elements ?? null;
    }

    function isAllowedChildExpression(node) {
      switch (node.type) {
        case "Identifier": {
          const initializer = localVariable(node)?.defs.find(
            (definition) => definition.type === "Variable",
          )?.node.init;
          return initializer == null || isAllowedChildExpression(initializer);
        }
        case "MemberExpression": {
          const value = staticMemberValue(node);
          return value == null || isAllowedChildExpression(value);
        }
        case "ArrayExpression":
          return node.elements.every((element) =>
            element == null || (element.type !== "SpreadElement" && isAllowedChildExpression(element)),
          );
        case "BinaryExpression":
          return node.operator !== "+"
            || (isAllowedChildExpression(node.left) && isAllowedChildExpression(node.right));
        case "CallExpression": {
          if (isKnownTranslationCall(node)) return true;
          if (node.callee.type === "Identifier" && node.callee.name === "String") {
            return node.arguments.every((argument) =>
              argument.type !== "SpreadElement" && isAllowedChildExpression(argument),
            );
          }
          const joinedElements = staticJoinElements(node);
          if (joinedElements != null) {
            return joinedElements.every((element) =>
              element == null || (element.type !== "SpreadElement" && isAllowedChildExpression(element)),
            );
          }
          // Dynamic calls can produce values such as formatted timestamps.
          // Calls to a local binding could be a hidden static copy helper, so
          // only a verified next-intl translation is permitted there.
          return !(node.callee.type === "Identifier" && localVariable(node.callee)?.defs.some(
              (definition) => definition.type !== "ImportBinding",
            ));
        }
        case "Literal":
          return typeof node.value !== "string" || isNonCopyText(node.value, allow);
        case "TemplateLiteral":
          return isNonCopyTemplate(node, allow);
        case "ConditionalExpression":
          return isAllowedChildExpression(node.consequent) && isAllowedChildExpression(node.alternate);
        case "LogicalExpression":
          return isAllowedChildExpression(node.left) && isAllowedChildExpression(node.right);
        case "TSAsExpression":
        case "TSTypeAssertion":
        case "TypeCastExpression":
          return isAllowedChildExpression(node.expression);
        default:
          // Expressions other than the static forms above represent runtime
          // data or JSX, which this lightweight rule cannot classify safely.
          return true;
      }
    }

    function staticObjectExpression(node) {
      if (node.type === "ObjectExpression") return node;
      if (node.type === "Identifier") {
        const initializer = localVariable(node)?.defs.find((def) => def.type === "Variable")?.node.init;
        return initializer?.type === "ObjectExpression" ? initializer : null;
      }
      if (node.type === "MemberExpression") {
        const value = staticMemberValue(node);
        return value?.type === "ObjectExpression" ? value : null;
      }
      return null;
    }

    function isAllowedSpreadArgument(node) {
      const object = staticObjectExpression(node);
      if (object == null) return node.type !== "CallExpression";

      return object.properties.every((property) => {
        if (property.type === "SpreadElement") return isAllowedSpreadArgument(property.argument);
        const name = objectPropertyName(property);
        return !name || !COPY_ATTRIBUTES.has(name) || isAllowedExpression(
          property.value,
          allow,
          isAllowedIdentifier,
          isAllowedMemberExpression,
          isKnownTranslationCall,
        );
      });
    }

    function report(node) {
      context.report({ node, messageId: "untranslatedCopy" });
    }

    return {
      JSXText(node) {
        if (!isNonCopyText(node.value, allow)) report(node);
      },
      JSXExpressionContainer(node) {
        if (node.parent.type !== "JSXAttribute" && !isAllowedChildExpression(node.expression)) {
          report(node);
        }
      },
      JSXAttribute(node) {
        const name = attributeName(node);
        if (!name || !COPY_ATTRIBUTES.has(name) || !node.value) return;

        if (node.value.type === "Literal") {
          if (typeof node.value.value === "string" && !isNonCopyText(node.value.value, allow)) {
            report(node.value);
          }
          return;
        }

        if (
          node.value.type === "JSXExpressionContainer"
          && !isAllowedExpression(
            node.value.expression,
            allow,
            isAllowedIdentifier,
            isAllowedMemberExpression,
            isKnownTranslationCall,
          )
        ) {
          report(node.value);
        }
      },
      JSXSpreadAttribute(node) {
        if (!isAllowedSpreadArgument(node.argument)) report(node);
      },
    };
  },
};

export default rule;
