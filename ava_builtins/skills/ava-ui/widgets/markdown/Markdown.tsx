// Markdown.tsx — React Markdown renderer with LaTeX + code highlight.
//
// Usage:
//   import { Markdown } from './Markdown';
//   <Markdown source={postMd} />
//
// Deps (add to your Vite/Next project):
//   npm i react-markdown remark-math rehype-katex remark-gfm rehype-highlight katex highlight.js
//
// Then import katex CSS once in your app entry (e.g. main.tsx):
//   import 'katex/dist/katex.min.css';
//   import 'highlight.js/styles/github.css';
//
// Inline `$...$` and display `$$...$$` LaTeX both supported via remark-math
// + rehype-katex. GFM tables / strikethrough / task lists via remark-gfm.
// Code highlighting via rehype-highlight (highlight.js auto-detect).
//
// Relative images (`![](images/1.jpg)`) resolve against the page-server
// root — same as the HTML version.

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';

interface Props {
  source: string;
  className?: string;
}

export function Markdown({ source, className }: Props) {
  return (
    <div className={className} style={{ maxWidth: 800, margin: '1em auto', padding: '0 1em', lineHeight: 1.6 }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex, rehypeHighlight]}
        components={{
          img: ({ node: _node, ...rest }) => (
            <img {...rest} style={{ maxWidth: '100%', height: 'auto' }} />
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
