import type {
  Emphasis,
  Link,
  Nodes,
  Parents,
  PhrasingContent,
  Root,
  Strong,
  Text,
} from "mdast";
import type { VFile } from "vfile";

interface DelimiterRun {
  marker: "*" | "_";
  start: number;
  length: number;
}

type DelimiterWrapper = Emphasis | Strong;

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

function lastMatchingDelimiterRun(
  value: string,
  target: DelimiterRun,
): DelimiterRun | undefined {
  const delimiter = target.marker.repeat(target.length);
  for (let start = value.length - target.length; start >= 0; start -= 1) {
    if (
      value.slice(start, start + target.length) === delimiter &&
      value[start - 1] !== target.marker &&
      value[start + target.length] !== target.marker
    ) {
      return { marker: target.marker, start, length: target.length };
    }
  }
  return undefined;
}

function isPlausibleOpener(
  value: string,
  run: DelimiterRun,
  allowAtEnd: boolean,
): boolean {
  const suffix = value.slice(run.start + run.length);
  return (
    (allowAtEnd && suffix.length === 0) ||
    /^[^\s\p{P}]/u.test(suffix)
  );
}

function stripLinkDelimiter(
  link: Link,
  child: Text,
  run: DelimiterRun,
): string {
  const urlPrefixLength = link.url.length - child.value.length;
  const remainder = child.value.slice(run.start + run.length);
  link.url = link.url.slice(0, run.start + urlPrefixLength);
  child.value = child.value.slice(0, run.start);
  return remainder;
}

function restoreDelimitedLink(
  link: Link,
  opener: Text,
  source: string,
):
  | { delimiterLength: number; remainder: string; suffix: string }
  | undefined {
  const child = literalLinkText(link, source);
  if (child === undefined) {
    return undefined;
  }

  const run = lastDelimiterRun(child.value);
  if (run === undefined) {
    return undefined;
  }

  const openerRun = lastMatchingDelimiterRun(opener.value, run);
  if (
    openerRun === undefined ||
    !isPlausibleOpener(opener.value, openerRun, true)
  ) {
    return undefined;
  }

  const suffix = opener.value.slice(openerRun.start + openerRun.length);
  opener.value = opener.value.slice(0, openerRun.start);
  return {
    delimiterLength: run.length,
    remainder: stripLinkDelimiter(link, child, run),
    suffix,
  };
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

function matchingWrapper(
  wrapper: DelimiterWrapper,
  run: DelimiterRun,
  source: string,
): boolean {
  const delimiterLengthMatches =
    (wrapper.type === "strong" && run.length >= 2) ||
    (wrapper.type === "emphasis" && run.length === 1);
  const startOffset = wrapper.position?.start.offset;
  return (
    delimiterLengthMatches &&
    startOffset !== undefined &&
    source[startOffset] === run.marker
  );
}

function sameTypeWrapper(
  wrapper: DelimiterWrapper,
  children: PhrasingContent[],
): DelimiterWrapper {
  return wrapper.type === "strong"
    ? { type: "strong", children }
    : { type: "emphasis", children };
}

function restoreLinkInWrapper(
  parent: Parents,
  parentIndex: number,
  wrapper: DelimiterWrapper,
  source: string,
): number {
  for (let linkIndex = 0; linkIndex < wrapper.children.length; linkIndex += 1) {
    const link = wrapper.children[linkIndex];
    if (link.type !== "link") {
      continue;
    }

    const linkText = literalLinkText(link, source);
    const run =
      linkText === undefined ? undefined : lastDelimiterRun(linkText.value);
    if (
      linkText === undefined ||
      run === undefined ||
      !matchingWrapper(wrapper, run, source)
    ) {
      continue;
    }

    const remainder = stripLinkDelimiter(link, linkText, run);
    if (remainder.length > 0) {
      appendRemainder(wrapper, linkIndex, remainder);
    }

    if (linkIndex + 1 >= wrapper.children.length) {
      return 0;
    }
    const tail = wrapper.children[linkIndex + 1];
    if (tail.type !== "text") {
      return 0;
    }
    const tailRun = lastMatchingDelimiterRun(tail.value, run);
    if (
      tailRun === undefined ||
      !isPlausibleOpener(tail.value, tailRun, false)
    ) {
      return 0;
    }

    const prefix = tail.value.slice(0, tailRun.start);
    tail.value = tail.value.slice(tailRun.start + tailRun.length);
    if (linkIndex === 0) {
      // The later opener starts a second span; split the parser's oversized
      // wrapper so prose between the two source spans keeps its source order.
      const trailingChildren = wrapper.children.splice(linkIndex + 1);
      const inserted: PhrasingContent[] = [];
      if (prefix.length > 0) {
        inserted.push({ type: "text", value: prefix });
      }
      inserted.push(sameTypeWrapper(wrapper, trailingChildren));
      parent.children.splice(parentIndex + 1, 0, ...inserted);
      return 0;
    }

    if (prefix.length > 0) {
      parent.children.splice(parentIndex, 0, { type: "text", value: prefix });
      return 1;
    }
    return 0;
  }
  return 0;
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
        const wrapperChildren: PhrasingContent[] = [];
        if (restored.suffix.length > 0) {
          wrapperChildren.push({ type: "text", value: restored.suffix });
        }
        wrapperChildren.push(child);
        parent.children[index] = {
          type: restored.delimiterLength >= 2 ? "strong" : "emphasis",
          children: wrapperChildren,
        };
        if (restored.remainder.length > 0) {
          appendRemainder(parent, index, restored.remainder);
        }
        continue;
      }
    }

    if (child.type === "strong" || child.type === "emphasis") {
      index += restoreLinkInWrapper(parent, index, child, source);
    }
    if (isParent(child)) {
      restoreLinksIn(child, source);
    }
  }
}

/**
 * Restore emphasis delimiters swallowed by GFM literal autolinks. Matching
 * opener and closer runs plus source positions keep bare URL paths intact.
 */
export default function remarkAutolinkDelimiter() {
  return (tree: Root, file: VFile): void => {
    restoreLinksIn(tree, file.toString());
  };
}
