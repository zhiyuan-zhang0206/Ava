import type { Link, Nodes, Parents, Root, Text } from "mdast";
import type { VFile } from "vfile";

const CJK_LINK_BOUNDARY =
  /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}|[\u2010-\u201F\u2026\u2E80-\u303F\u3040-\u30FF\u3300-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFE10-\uFE1F\uFE30-\uFE4F\uFF00-\uFFEF]/u;

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

function splitLiteralLink(link: Link, source: string): string | undefined {
  const child = literalLinkText(link, source);
  if (child === undefined) {
    return undefined;
  }

  const urlCut = link.url.search(CJK_LINK_BOUNDARY);
  if (urlCut === -1) {
    return undefined;
  }

  const textCut = urlCut - (link.url.length - child.value.length);
  const remainder = child.value.slice(textCut);
  link.url = link.url.slice(0, urlCut);
  child.value = child.value.slice(0, textCut);
  return remainder;
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

function splitLinksIn(parent: Parents, source: string): void {
  for (let index = 0; index < parent.children.length; index += 1) {
    const child = parent.children[index];
    if (child.type === "link") {
      const remainder = splitLiteralLink(child, source);
      if (remainder !== undefined && remainder.length > 0) {
        appendRemainder(parent, index, remainder);
      }
    }

    if (isParent(child)) {
      splitLinksIn(child, source);
    }
  }
}

/**
 * Truncate GFM literal autolinks at CJK, fullwidth, dash, or ellipsis text.
 * Raw non-ASCII characters require URL percent-encoding and are almost always
 * trailing prose in agent output. Explicit links and CommonMark full autolinks
 * are intentionally left untouched.
 */
export default function remarkCjkLinkBoundary() {
  return (tree: Root, file: VFile): void => {
    splitLinksIn(tree, file.toString());
  };
}
