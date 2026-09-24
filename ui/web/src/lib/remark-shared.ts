import type { Link, Nodes, Parents, Text } from "mdast";

export function isParent(node: Nodes): node is Parents {
  return "children" in node;
}

export function literalLinkText(link: Link, source: string): Text | undefined {
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

export function appendRemainder(parent: Parents, index: number, remainder: string): void {
  if (index + 1 < parent.children.length) {
    const following = parent.children[index + 1];
    if (following.type === "text") {
      following.value = remainder + following.value;
      return;
    }
  }
  parent.children.splice(index + 1, 0, { type: "text", value: remainder });
}
