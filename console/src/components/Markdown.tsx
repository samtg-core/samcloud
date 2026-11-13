'use client';

import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { useState } from 'react';
import { remarkRefLinks } from '@/lib/refLinks';

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

// A plane page (/tickets/N, /docs/N, /tasks/N) opens in place, and its title
// shows on hover — fetched once per ref, on first hover (#1082).
const PLANE_REF = /^\/(tickets|docs|tasks)\/(\d+)\/?$/;
const refTitles = new Map<string, Promise<string>>();

function refTitle(kind: string, id: string): Promise<string> {
  const key = `${kind}/${id}`;
  let p = refTitles.get(key);
  if (!p) {
    p = fetch(`/api/registry?path=${encodeURIComponent(`/api/v1/${kind}/${id}`)}`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : {}))
      .then((d: { summary?: string; title?: string }) => d.summary || d.title || '')
      .catch(() => '');
    refTitles.set(key, p);
  }
  return p;
}

function PlaneLink({ href, kind, id, ...props }: React.ComponentProps<'a'> & { kind: string; id: string }) {
  const [title, setTitle] = useState<string>();
  return (
    <a
      href={href}
      title={title}
      onMouseEnter={() => { if (title === undefined) refTitle(kind, id).then(setTitle); }}
      className="text-blue-400 hover:underline"
      {...props}
    />
  );
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
  a: ({ href, ...props }) => {
    const plane = typeof href === 'string' ? href.match(PLANE_REF) : null;
    if (plane) return <PlaneLink href={href} kind={plane[1]} id={plane[2]} {...props} />;
    return (
      <a
        href={fileSrc(typeof href === 'string' ? href : undefined)}
        className="text-blue-400 hover:underline"
        target="_blank"
        rel="noopener noreferrer"
        {...props}
      />
    );
  },
  ul: (props) => <ul className="list-disc pl-5 my-2 space-y-1" {...props} />,
  ol: (props) => <ol className="list-decimal pl-5 my-2 space-y-1" {...props} />,
  blockquote: (props) => <blockquote className="border-l-2 border-zinc-700 pl-3 my-2 text-zinc-500" {...props} />,
  code: (props) => <code className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-xs" {...props} />,
  pre: (props) => <pre className="overflow-x-auto rounded-md border border-zinc-800 bg-zinc-950 p-3 my-2 text-xs [&_code]:bg-transparent [&_code]:p-0" {...props} />,
  // A wide table scrolls inside its own box instead of widening the page (#1082).
  table: (props) => (
    <div className="my-2 max-w-full overflow-x-auto">
      <table className="text-xs border-collapse" {...props} />
    </div>
  ),
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
    // overflow-wrap:anywhere, not break-words: only `anywhere` lets a long URL or
    // path shrink its flex parent, so it wraps instead of widening the page (#1082).
    <div className={`min-w-0 text-sm leading-relaxed [overflow-wrap:anywhere] ${muted ? 'text-zinc-400' : 'text-zinc-300'}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks, remarkRefLinks]} components={comps}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
