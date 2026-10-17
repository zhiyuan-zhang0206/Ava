// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
import type { PluggableList } from "unified";

export interface MarkdownMath {
  remark: PluggableList;
  rehype: PluggableList;
}

const defaultImport = async (): Promise<MarkdownMath> => {
  const [remarkMath, rehypeKatex] = await Promise.all([
    import("remark-math"),
    import("rehype-katex"),
    import("katex/dist/katex.min.css"),
  ]);
  return {
    remark: [remarkMath.default],
    rehype: [[rehypeKatex.default, { throwOnError: false }]],
  };
};

let importMath: () => Promise<MarkdownMath> = defaultImport;
let mathPromise: Promise<MarkdownMath> | null = null;

export function loadMarkdownMath(): Promise<MarkdownMath> {
  if (mathPromise !== null) return mathPromise;
  const promise = importMath().catch((error: unknown) => {
    if (mathPromise === promise) mathPromise = null;
    throw error;
  });
  mathPromise = promise;
  return promise;
}

export function __resetMarkdownMathForTests(): void {
  mathPromise = null;
}

export function __setMarkdownMathImportForTests(loader: (() => Promise<MarkdownMath>) | null): void {
  importMath = loader ?? defaultImport;
}
