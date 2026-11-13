'use client';

// The inbox feed (#1082): one card per ticket, task or doc, newest activity
// first, with read and star kept per card (GET /inbox/feed, POST /inbox/marks).
// "For me" — what was addressed to you, assigned to you or yours — is the
// default; everything else you can see is one chip away. Reading a card keeps
// it in the feed, dimmed; it comes back to unread when someone else acts on it.

import { timeAgo } from '@/lib/time';
import { useEffect, useState } from 'react';
import { useScopeFilter } from '@/lib/scopeFilter';
import { MdSnippet } from '@/components/MdSnippet';
import { stashInboxQueue } from '@/components/InboxQueuePill';

type Kind = 'ticket' | 'task' | 'doc';
type Filter = 'mine' | 'unread' | 'starred' | 'all';

export interface FeedItem {
  kind: Kind;
  id: number;
  title: string;
  status: string;
  href: string;
  project?: string;
  doc_kind?: string;
  assignee?: string | null;
  updated_at: string;
  comments: number;
  latest: { what: string; by: string | null; at: string; text: string };
  unread: boolean;
  starred: boolean;
  mine: boolean;
  mention: boolean;
}

interface FeedBody {
  items: FeedItem[];
  total: number;
  counts: Record<Filter, number>;
}

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'mine', label: 'For me' },
  { key: 'unread', label: 'Unread' },
  { key: 'starred', label: 'Starred' },
  { key: 'all', label: 'Everything' },
];
const KINDS: { key: Kind; label: string }[] = [
  { key: 'ticket', label: 'tickets' },
  { key: 'task', label: 'tasks' },
  { key: 'doc', label: 'docs' },
];
const ACCENT: Record<Kind, string> = {
  ticket: 'text-orange-400',
  task: 'text-purple-400',
  doc: 'text-blue-400',
};
const PAGE = 30;

async function reg(path: string, init?: RequestInit) {
  return fetch(`/api/registry?path=${encodeURIComponent(path)}`, {
    cache: 'no-store',
    ...(init || {}),
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  });
}

function mark(items: { kind: Kind; id: number }[], change: { read?: boolean; starred?: boolean }, keepalive = false) {
  return reg('/api/v1/inbox/marks', { method: 'POST', body: JSON.stringify({ items, ...change }), keepalive });
}

function label(it: FeedItem): string {
  return it.kind === 'ticket' ? `#${it.id}` : `${it.kind} ${it.id}`;
}

function FeedCard({ it, onChange, onOpen }: { it: FeedItem; onChange: (patch: Partial<FeedItem>) => void; onOpen: () => void }) {
  const ref = { kind: it.kind, id: it.id };
  const who = it.latest.by ? ` by ${it.latest.by}` : '';
  return (
    <div className={`flex items-start gap-3 rounded-lg border p-3 ${
      it.mention && it.unread ? 'border-amber-500/30 bg-amber-500/5'
        : it.unread ? 'border-zinc-700 bg-zinc-900/60' : 'border-zinc-800/70 bg-zinc-900/20'}`}>
      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${it.unread ? 'bg-emerald-500' : 'bg-transparent'}`} />
      <a
        href={it.href}
        onClick={() => { if (it.unread) mark([ref], { read: true }, true).catch(() => {}); onOpen(); }}
        className={`block min-w-0 flex-1 ${it.unread ? '' : 'opacity-70'}`}
      >
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {it.mention && it.unread && (
            <span className="rounded border border-amber-500/40 px-1 text-[10px] font-semibold uppercase tracking-wide text-amber-400">@you</span>
          )}
          <span className={`font-mono text-xs ${ACCENT[it.kind]}`}>{label(it)}</span>
          <span className={`min-w-0 ${it.unread ? 'text-zinc-100' : 'text-zinc-300'}`}>{it.title}</span>
          <span className="text-xs text-zinc-600">
            {it.status.replace('_', ' ')}
            {it.project ? ` · ${it.project}` : ''}
            {it.comments ? ` · ${it.comments} comment${it.comments > 1 ? 's' : ''}` : ''}
          </span>
        </div>
        <div className="mt-0.5 text-xs text-zinc-500">
          {it.latest.what}{who} · {timeAgo(it.latest.at || it.updated_at)}
        </div>
        <MdSnippet text={it.latest.text} strong={it.unread} />
      </a>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <button
          onClick={() => { onChange({ starred: !it.starred }); mark([ref], { starred: !it.starred }); }}
          title={it.starred ? 'Unstar' : 'Star: keep it under Starred'}
          className={`px-1 text-base leading-none ${it.starred ? 'text-amber-400' : 'text-zinc-600 hover:text-amber-300'}`}
        >
          {it.starred ? '★' : '☆'}
        </button>
        <button
          onClick={() => { onChange({ unread: !it.unread }); mark([ref], { read: it.unread }); }}
          title={it.unread ? 'Mark read' : 'Mark unread'}
          className="rounded border border-zinc-700 px-1.5 py-0.5 text-[11px] text-zinc-500 transition-colors hover:text-white"
        >
          {it.unread ? 'read' : 'unread'}
        </button>
      </div>
    </div>
  );
}

export function Feed({ windowHours, refreshTick }: { windowHours: number; refreshTick: number }) {
  const scope = useScopeFilter();
  const [filter, setFilter] = useState<Filter>('mine');
  const [kinds, setKinds] = useState<Kind[]>(['ticket', 'task', 'doc']);
  const [shown, setShown] = useState(PAGE);
  const [body, setBody] = useState<FeedBody | null>(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      const qs = new URLSearchParams({
        filter, kind: kinds.join(','), window_hours: String(windowHours), limit: String(shown),
      });
      if (scope) qs.set('group_scope', scope);
      const res = await reg(`/api/v1/inbox/feed?${qs}`);
      if (alive && res.ok) setBody(await res.json());
    }
    load();
    const id = setInterval(load, 60000);
    return () => { alive = false; clearInterval(id); };
  }, [filter, kinds, windowHours, shown, scope, refreshTick]);

  function patch(i: number, p: Partial<FeedItem>) {
    setBody((b) => b && { ...b, items: b.items.map((x, j) => (j === i ? { ...x, ...p } : x)) });
  }

  function toggleKind(k: Kind) {
    setKinds((ks) => (ks.includes(k) ? (ks.length > 1 ? ks.filter((x) => x !== k) : ks) : [...ks, k]));
    setShown(PAGE);
  }

  const items = body?.items ?? [];
  // Opening a card queues the rest for the floating next-pill (#391).
  const stashFor = (href: string) => () =>
    stashInboxQueue(items.filter((x) => x.href !== href)
      .map((x) => ({ href: x.href, label: `${label(x)} ${x.title.slice(0, 40)}` })));

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">Feed</h3>
        <span className="inline-flex overflow-hidden rounded-md border border-zinc-800">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => { setFilter(f.key); setShown(PAGE); }}
              className={`px-2 py-0.5 text-xs ${filter === f.key ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-white'}`}
            >
              {f.label}{body ? ` ${body.counts[f.key]}` : ''}
            </button>
          ))}
        </span>
        <span className="inline-flex gap-1">
          {KINDS.map((k) => (
            <button
              key={k.key}
              onClick={() => toggleKind(k.key)}
              className={`rounded border px-1.5 py-0.5 text-xs ${kinds.includes(k.key)
                ? `border-zinc-700 ${ACCENT[k.key]}` : 'border-zinc-800 text-zinc-700 line-through'}`}
            >
              {k.label}
            </button>
          ))}
        </span>
      </div>

      {!body ? (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-center text-xs text-zinc-700">loading…</div>
      ) : items.length === 0 ? (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-center text-sm text-zinc-600">
          {filter === 'starred' ? 'Nothing starred yet.' : filter === 'unread' ? 'All read.' : 'Nothing in this window.'}
        </div>
      ) : (
        items.map((it, i) => (
          <FeedCard key={`${it.kind}:${it.id}`} it={it} onChange={(p) => patch(i, p)} onOpen={stashFor(it.href)} />
        ))
      )}
      {body && body.total > items.length && (
        <button
          onClick={() => setShown((n) => n + PAGE)}
          className="w-full rounded-lg border border-zinc-800 py-2 text-xs text-zinc-500 hover:text-white"
        >
          Show more ({body.total - items.length} not shown)
        </button>
      )}
    </section>
  );
}
