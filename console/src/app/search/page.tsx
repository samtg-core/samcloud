'use client';

// Search across planes (#1082): one box over tickets, tasks and docs —
// titles, bodies and comments — with the same filters on all three
// (GET /api/v1/search). Reached from the top bar's search icon, the ≡ menu, ⌘K or "/".
// The query and filters live in the URL, so a search can be shared or reloaded.

import { Suspense, useEffect, useRef, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { timeAgo } from '@/lib/time';
import { useScopeFilter } from '@/lib/scopeFilter';

type Kind = 'ticket' | 'task' | 'doc';

interface Hit {
  kind: Kind;
  id: number;
  title: string;
  status: string | null;
  href: string;
  assignee: string | null;
  project: string | null;
  updated_at: string;
  match: 'id' | 'title' | 'body' | 'comment' | null;
  snippet: string;
}

interface Body { items: Hit[]; total: number; counts: Partial<Record<Kind, number>>; terms: string[] }

const KINDS: { key: Kind; label: string }[] = [
  { key: 'ticket', label: 'tickets' },
  { key: 'task', label: 'tasks' },
  { key: 'doc', label: 'docs' },
];
const ACCENT: Record<Kind, string> = { ticket: 'text-orange-400', task: 'text-purple-400', doc: 'text-blue-400' };
const STATUSES = [
  { value: '', label: 'Any status' },
  { value: 'active', label: 'Active (still moving)' },
  { value: 'blocked', label: 'Blocked tasks' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'open', label: 'Open tickets' },
  { value: 'todo', label: 'To do' },
  { value: 'resolved,closed,done,cancelled,archived,superseded', label: 'Finished' },
];
const WINDOWS = [
  { value: '', label: 'Any time' },
  { value: '24', label: 'Last 24h' },
  { value: '168', label: 'Last 7d' },
  { value: '720', label: 'Last 30d' },
];
const PAGE = 30;
const select = 'min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none';

function Highlight({ text, terms }: { text: string; terms: string[] }) {
  const words = terms.filter((t) => !t.startsWith('#'));
  if (!words.length || !text) return <>{text}</>;
  const re = new RegExp(`(${words.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
  return <>{text.split(re).map((part, i) => (i % 2 ? <mark key={i} className="rounded bg-amber-500/25 px-0.5 text-amber-200">{part}</mark> : part))}</>;
}

function SearchInner() {
  const params = useSearchParams();
  const router = useRouter();
  const scope = useScopeFilter();
  const [q, setQ] = useState(params.get('q') || '');
  const [kinds, setKinds] = useState<Kind[]>(() => {
    const k = (params.get('kind') || '').split(',').filter((x): x is Kind => ['ticket', 'task', 'doc'].includes(x));
    return k.length ? k : ['ticket', 'task', 'doc'];
  });
  const [status, setStatus] = useState(params.get('status') || '');
  const [mine, setMine] = useState(params.get('assignee') === ':me');
  const [hours, setHours] = useState(params.get('updated_hours') || '');
  const [shown, setShown] = useState(PAGE);
  const [body, setBody] = useState<Body | null>(null);
  const [loading, setLoading] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { input.current?.focus(); }, []);

  // Debounced: the query and filters go to the URL and the registry together.
  useEffect(() => {
    let alive = true;
    const qs = new URLSearchParams();
    if (q.trim()) qs.set('q', q.trim());
    if (kinds.length < 3) qs.set('kind', kinds.join(','));
    if (status) qs.set('status', status);
    if (mine) qs.set('assignee', ':me');
    if (hours) qs.set('updated_hours', hours);
    const url = qs.toString();
    const t = setTimeout(async () => {
      router.replace(url ? `/search?${url}` : '/search', { scroll: false });
      if (!q.trim() && !status && !mine && !hours) { setBody(null); return; }
      const api = new URLSearchParams(qs);
      api.set('limit', String(shown));
      if (scope) api.set('group_scope', scope);
      setLoading(true);
      try {
        const res = await fetch(`/api/registry?path=${encodeURIComponent(`/api/v1/search?${api}`)}`, { cache: 'no-store' });
        if (alive && res.ok) setBody(await res.json());
      } finally {
        if (alive) setLoading(false);
      }
    }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [q, kinds, status, mine, hours, shown, scope, router]);

  function toggleKind(k: Kind) {
    setKinds((ks) => (ks.includes(k) ? (ks.length > 1 ? ks.filter((x) => x !== k) : ks) : [...ks, k]));
    setShown(PAGE);
  }

  const items = body?.items ?? [];
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <h2 className="text-2xl font-bold text-zinc-100">Search</h2>
      <input
        ref={input}
        value={q}
        onChange={(e) => { setQ(e.target.value); setShown(PAGE); }}
        placeholder="Search tickets, tasks and docs: titles, bodies, comments, #id…"
        className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2.5 text-zinc-100 placeholder-zinc-600 focus:border-zinc-500 focus:outline-none"
      />
      <div className="flex flex-wrap items-center gap-2">
        {KINDS.map((k) => (
          <button
            key={k.key}
            onClick={() => toggleKind(k.key)}
            className={`rounded border px-2 py-1 text-xs ${kinds.includes(k.key)
              ? `border-zinc-700 ${ACCENT[k.key]}` : 'border-zinc-800 text-zinc-700 line-through'}`}
          >
            {k.label}{body?.counts?.[k.key] !== undefined ? ` ${body.counts[k.key]}` : ''}
          </button>
        ))}
        <button
          onClick={() => { setMine((m) => !m); setShown(PAGE); }}
          className={`rounded border px-2 py-1 text-xs ${mine ? 'border-zinc-600 bg-zinc-700 text-white' : 'border-zinc-800 text-zinc-500'}`}
        >
          assigned to me
        </button>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:flex">
        <select value={status} onChange={(e) => { setStatus(e.target.value); setShown(PAGE); }} className={select}>
          {STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
        <select value={hours} onChange={(e) => { setHours(e.target.value); setShown(PAGE); }} className={select}>
          {WINDOWS.map((w) => <option key={w.value} value={w.value}>{w.label}</option>)}
        </select>
      </div>

      {!body ? (
        <p className="py-6 text-center text-sm text-zinc-600">
          {loading ? 'Searching…' : 'Type to search, or pick a filter. ⌘K or / opens this from any page.'}
        </p>
      ) : items.length === 0 ? (
        <p className="py-6 text-center text-sm text-zinc-600">Nothing matches.</p>
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-zinc-600">{body.total} found{loading ? ' · updating…' : ''}</p>
          {items.map((h) => (
            <a key={`${h.kind}:${h.id}`} href={h.href}
               className="block min-w-0 rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 transition-colors hover:bg-zinc-800/30">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className={`font-mono text-xs ${ACCENT[h.kind]}`}>{h.kind === 'ticket' ? `#${h.id}` : `${h.kind} ${h.id}`}</span>
                <span className="min-w-0 text-zinc-200"><Highlight text={h.title} terms={body.terms} /></span>
                <span className="text-xs text-zinc-600">
                  {(h.status || '').replace('_', ' ')}
                  {h.project ? ` · ${h.project}` : ''}
                  {h.assignee ? ` · ${h.assignee}` : ''}
                  {' · '}{timeAgo(h.updated_at)}
                </span>
              </div>
              {h.snippet && (
                <p className="mt-1 line-clamp-2 text-xs text-zinc-400 [overflow-wrap:anywhere]">
                  {h.match === 'comment' && <span className="text-zinc-600">in a comment · </span>}
                  <Highlight text={h.snippet} terms={body.terms} />
                </p>
              )}
            </a>
          ))}
          {body.total > items.length && (
            <button onClick={() => setShown((n) => n + PAGE)}
                    className="w-full rounded-lg border border-zinc-800 py-2 text-xs text-zinc-500 hover:text-white">
              Show more ({body.total - items.length} not shown)
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function SearchPage() {
  // useSearchParams() must sit under a Suspense boundary.
  return (
    <Suspense fallback={<div className="p-8 text-center text-sm text-zinc-600">Loading…</div>}>
      <SearchInner />
    </Suspense>
  );
}
