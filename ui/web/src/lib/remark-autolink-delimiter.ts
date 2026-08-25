import type { Link, Nodes, Parents, Root, Text } from "mdast";
import type { VFile } from "vfile";

interface DelimiterRun {
  marker: "*" | "_";
  start: number;
  length: number;
}

function isParent(node: Nodes): node is Parents {
  return "children" in node;
}

function literalLinkText(link: Link, source: string): Text | undefined {
  const child = link.children[0];
  if (
    link.children.length !== 1 ||
    child.type !== "text" ||
    link.title !== null ||
    link.data !== undefined
  ) {
    return undefined;
  }

  const isHttpLiteral =
    /^https?:\/\//i.test(child.value) && link.url === child.value;
  const isWwwLiteral =
    child.value.startsWith("www.") && link.url === `http://${child.value}`;
  if (!isHttpLiteral && !isWwwLiteral) {
    return undefined;
  }

  const startOffset = link.position?.start.offset;
  if (startOffset !== undefined && source[startOffset] === "<") {
    return undefined;
  }
  return child;
}

function lastDelimiterRun(value: string): DelimiterRun | undefined {
  const lastStar = value.lastIndexOf("*");
  const lastUnderscore = value.lastIndexOf("_");
  const end = Math.max(lastStar, lastUnderscore);
  if (end === -1) {
    return undefined;
  }

  const marker = value[end] as "*" | "_";
  let start = end;
  while (start > 0 && value[start - 1] === marker) {
    start -= 1;
  }
  return { marker, start, length: end - start + 1 };
}

function hasExactTrailingRun(text: Text, run: DelimiterRun): boolean {
  const openerStart = text.value.length - run.length;
  return (
    openerStart >= 0 &&
    text.value.slice(openerStart) === run.marker.repeat(run.length) &&
    (openerStart === 0 || text.value[openerStart - 1] !== run.marker)
  );
}

function restoreDelimitedLink(
  link: Link,
  opener: Text,
  source: string,
): { delimiterLength: number; remainder: string } | undefined {
  const child = literalLinkText(link, source);
  if (child === undefined) {
    return undefined;
  }

  const run = lastDelimiterRun(child.value);
  if (run === undefined || !hasExactTrailingRun(opener, run)) {
    return undefined;
  }

  const urlPrefixLength = link.url.length - child.value.length;
  const remainder = child.value.slice(run.start + run.length);
  link.url = link.url.slice(0, run.start + urlPrefixLength);
  child.value = child.value.slice(0, run.start);
  opener.value = opener.value.slice(0, -run.length);
  return { delimiterLength: run.length, remainder };
}

function appendRemainder(parent: Parents, index: number, remainder: string): void {
  if (index + 1 < parent.children.length) {
    const following = parent.children[index + 1];
    if (following.type === "text") {
      following.value = remainder + following.value;
      return;
    }
  }
  parent.children.splice(index + 1, 0, { type: "text", value: remainder });
}

function restoreLinksIn(parent: Parents, source: string): void {
  for (let index = 0; index < parent.children.length; index += 1) {
    const child = parent.children[index];
    const opener = index > 0 ? parent.children[index - 1] : undefined;
    if (child.type === "link" && opener?.type === "text") {
      const restored = restoreDelimitedLink(child, opener, source);
      if (restored !== undefined) {
        if (opener.value.length === 0) {
          parent.children.splice(index - 1, 1);
          index -= 1;
        }
        parent.children[index] = {
          type: restored.delimiterLength >= 2 ? "strong" : "emphasis",
          children: [child],
        };
        if (restored.remainder.length > 0) {
          appendRemainder(parent, index, restored.remainder);
        }
        continue;
      }
    }

    if (isParent(child)) {
      restoreLinksIn(child, source);
    }
  }
}

/**
 * Restore emphasis delimiters that GFM literal autolinks swallowed before
 * adjacent prose. Matching opener and closer runs keep bare URL paths intact.
 */
export default function remarkAutolinkDelimiter() {
  return (tree: Root, file: VFile): void => {
    restoreLinksIn(tree, file.toString());
  };
}
