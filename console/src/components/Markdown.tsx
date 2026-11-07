'use client';

import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';

/**
 * Sanitized markdown for ticket bodies/comments (ticket #151).
 *
 * Safe by construction: without rehype-raw, react-markdown drops raw HTML
 * from the source instead of injecting it — no dangerouslySetInnerHTML
 * anywhere. remark-gfm adds tables/strikethrough/task lists/autolinks;
 * remark-breaks keeps single newlines as line breaks (GitHub-comment style),
 * which is how ticket text is typically written.
 */

/**
 * Registry file URLs cannot be fetched by a browser: /api/v1/files/{id}/raw
 * requires an Authorization header and an <img> cannot send one. The console
 * proxy at /api/files/{id}/raw authenticates the session cookie instead.
 *
 * Rewriting here means an author can write the natural registry path — which is
 * what the API docs show and what every agent will reach for — and it still
 * renders. Without this, "ordinary markdown" was not ordinary: it worked only
 * if you happened to know to use a different path (#313).
 */
function fileSrc(src?: string): string | undefined {
  if (!src) return src;
  return src.replace(/^(?:https?:\/\/[^/]+)?\/api\/v1\/files\/(\d+)\/raw/, '/api/files/$1/raw');
}

const components: Components = {
  img: ({ src, alt, ...rest }) => {
    const resolved = fileSrc(typeof src === 'string' ? src : undefined);
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={resolved}
        alt={alt || ''}
        loading="lazy"
        // Height-capped so a full-res screenshot doesn't take over the thread
        // (#377); click opens the raw file. onClick instead of a wrapping <a>
        // because [![img](...)](url) would otherwise nest anchors.
        onClick={() => resolved && window.open(resolved, '_blank', 'noopener')}
        className="my-2 max-w-full max-h-80 w-auto h-auto rounded-md border border-zinc-800 cursor-zoom-in"
        {...rest}
      />
    );
  },
  h1: (props) => <h1 className="text-lg font-semibold text-zinc-100 mt-4 first:mt-0" {...props} />,
  h2: (props) => <h2 className="text-base font-semibold text-zinc-100 mt-4 first:mt-0" {...props} />,
  h3: (props) => <h3 className="text-sm font-semibold text-zinc-100 mt-3 first:mt-0" {...props} />,
  h4: (props) => <h4 className="text-sm font-semibold text-zinc-200 mt-3 first:mt-0" {...props} />,
  p: (props) => <p className="my-2 first:mt-0 last:mb-0" {...props} />,
  // href runs through the same proxy rewrite as img src: authors link file
  // evidence as [label](/api/v1/files/N/raw) at least as often as they embed
  // it, and an unrewritten anchor opens the bare registry URL — which a
  // browser cannot authenticate, so every such click 401'd (2026-08-27).
  a: ({ href, ...props }) => (
    <a
      href={fileSrc(typeof href === 'string' ? href : undefined)}
      className="text-blue-400 hover:underline"
      target="_blank"
      rel="noopener noreferrer"
      {...props}
    />
  ),
  ul: (props) => <ul className="list-disc pl-5 my-2 space-y-1" {...props} />,
  ol: (props) => <ol className="list-decimal pl-5 my-2 space-y-1" {...props} />,
  blockquote: (props) => <blockquote className="border-l-2 border-zinc-700 pl-3 my-2 text-zinc-500" {...props} />,
  code: (props) => <code className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-xs" {...props} />,
  pre: (props) => <pre className="overflow-x-auto rounded-md border border-zinc-800 bg-zinc-950 p-3 my-2 text-xs [&_code]:bg-transparent [&_code]:p-0" {...props} />,
  table: (props) => <table className="my-2 text-xs border-collapse" {...props} />,
  th: (props) => <th className="border border-zinc-800 bg-zinc-800/50 px-2 py-1 text-left font-semibold text-zinc-200" {...props} />,
  td: (props) => <td className="border border-zinc-800 px-2 py-1" {...props} />,
  hr: (props) => <hr className="border-zinc-800 my-3" {...props} />,
  strong: (props) => <strong className="font-semibold text-zinc-200" {...props} />,
};

export function Markdown({ children, muted = false, flatLinks = false }: { children: string; muted?: boolean; flatLinks?: boolean }) {
  // flatLinks: for previews rendered INSIDE an <a> card — links become styled
  // text so anchors never nest (invalid HTML + hydration noise).
  const comps = flatLinks
    ? { ...components, a: (props: React.ComponentProps<'a'>) => <span className="text-blue-400">{props.children}</span> }
    : components;
  return (
    <div className={`text-sm leading-relaxed break-words ${muted ? 'text-zinc-400' : 'text-zinc-300'}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={comps}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
