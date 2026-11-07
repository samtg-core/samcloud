'use client';

import { timeAgo } from '@/lib/time';
import { Suspense, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import type { Doc } from '@/lib/registry';
import { CreateDocForm } from './CreateDocForm';
import { useScopeFilter } from '@/lib/scopeFilter';


const STATUS_FILTERS = ['active', 'draft', 'superseded', 'archived'];

export default function DocsPage() {
  return (
    <Suspense fallback={<div className="max-w-4xl mx-auto p-8 text-center text-zinc-600 text-sm">Loading docs…</div>}>
      <DocsInner />
    </Suspense>
  );
}

function DocsInner() {
  const searchParams = useSearchParams();
  const filterStatus = searchParams.get('status') || undefined;
  const filterKind = searchParams.get('kind') || undefined;

  const [docs, setDocs] = useState<Doc[]>([]);
  const [loading, setLoading] = useState(true);
  const [sortBy, setSortBy] = useState('updated:desc');
  const [groupF, setGroupF] = useState('');
  const [collapseTick, setCollapseTick] = useState<{ mode: boolean; n: number }>({ mode: false, n: 0 });
  const scope = useScopeFilter();  // H2 (#380)

  useEffect(() => {
    let alive = true;
    async function load() {
      const qs = new URLSearchParams();
      if (filterStatus) qs.set('status', filterStatus);
      if (filterKind) qs.set('kind', filterKind);
      if (scope) qs.set('group_scope', scope);
      const path = '/api/v1/docs' + (qs.toString() ? `?${qs.toString()}` : '');
      try {
        const res = await fetch(`/api/registry?path=${encodeURIComponent(path)}`, { cache: 'no-store' });
        const data = res.ok ? await res.json() : { docs: [] };
        if (alive) setDocs(Array.isArray(data?.docs) ? data.docs : []);
      } catch {
        if (alive) setDocs([]);
      } finally {
        if (alive) setLoading(false);
      }
    }
    setLoading(true);
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, [filterStatus, filterKind, scope]);

  function filterHref(status?: string) {
    const qs = new URLSearchParams();
    if (status) qs.set('status', status);
    if (filterKind) qs.set('kind', filterKind);
    return qs.toString() ? `/docs?${qs.toString()}` : '/docs';
  }

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Docs</h2>
          <p className="text-zinc-500 mt-1">Versioned shared knowledge — a sibling to tickets</p>
        </div>
        <a href="/tickets" className="text-sm text-zinc-400 hover:text-white transition-colors shrink-0">
          Tickets
        </a>
      </div>

      {/* Status filter */}
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <a href={filterHref(undefined)}
           className={`rounded-full border px-3 py-1 ${!filterStatus ? 'border-zinc-500 text-zinc-100' : 'border-zinc-800 text-zinc-500 hover:text-zinc-300'}`}>
          all
        </a>
        {STATUS_FILTERS.map(s => (
          <a key={s} href={filterHref(s)}
             className={`rounded-full border px-3 py-1 ${filterStatus === s ? 'border-zinc-500 text-zinc-100' : 'border-zinc-800 text-zinc-500 hover:text-zinc-300'}`}>
            {s}
          </a>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}
                className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-100 focus:border-zinc-600 focus:outline-none">
          <option value="updated:desc">recently updated</option>
          <option value="updated:asc">least recently updated</option>
          <option value="created:desc">newest created</option>
          <option value="created:asc">oldest created</option>
          <option value="title:asc">title A–Z</option>
        </select>
        <select value={groupF} onChange={(e) => setGroupF(e.target.value)}
                className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-100 focus:border-zinc-600 focus:outline-none">
          <option value="">any group</option>
          {[...new Set(docs.map(d => d.group_scope).filter(Boolean))].sort().map(g => (
            <option key={g as string} value={g as string}>group: {g}</option>
          ))}
        </select>
        <span className="flex-1" />
        <button onClick={() => setCollapseTick(t => ({ mode: true, n: t.n + 1 }))}
                className="text-xs text-zinc-500 hover:text-white border border-zinc-800 rounded-md px-2 py-1">collapse all</button>
        <button onClick={() => setCollapseTick(t => ({ mode: false, n: t.n + 1 }))}
                className="text-xs text-zinc-500 hover:text-white border border-zinc-800 rounded-md px-2 py-1">expand all</button>
      </div>

      <CreateDocForm />

      <div className="space-y-3">
        {loading ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">Loading…</div>
        ) : docs.length > 0 ? (
          buildTree(sortDocs(docs.filter(d =>
            !groupF ? true : d.group_scope === groupF
          ), sortBy)).map((n) => <DocNode key={n.doc.id} node={n} depth={0} collapse={collapseTick} />)
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">
            {filterStatus ? `No ${filterStatus} docs` : 'No docs yet'}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── doc hierarchy (H3, doc-plane nesting, Notion-style) ─────────────────────
// The list stays one flat fetch; nesting is assembled client-side. A child
// whose parent isn't in the visible/filtered set renders top-level rather
// than vanishing, so filters and scope narrowing never hide docs.

interface DocTreeNode { doc: Doc; children: DocTreeNode[] }

function sortDocs(docs: Doc[], sortBy: string): Doc[] {
  const [field, dir] = sortBy.split(':');
  const key = (d: Doc) => field === 'created' ? d.created_at : field === 'title' ? d.title.toLowerCase() : d.updated_at;
  return [...docs].sort((a, b) => {
    const cmp = String(key(a) ?? '').localeCompare(String(key(b) ?? ''));
    return dir === 'asc' ? cmp : -cmp;
  });
}

function buildTree(docs: Doc[]): DocTreeNode[] {
  const ids = new Set(docs.map((d) => d.id));
  const nodes = new Map<number, DocTreeNode>(docs.map((d) => [d.id, { doc: d, children: [] }]));
  const roots: DocTreeNode[] = [];
  for (const d of docs) {
    const pid = d.parent_doc_id;
    if (pid && ids.has(pid) && pid !== d.id) {
      nodes.get(pid)!.children.push(nodes.get(d.id)!);
    } else {
      roots.push(nodes.get(d.id)!);
    }
  }
  return roots;
}

function DocNode({ node, depth, collapse }: { node: DocTreeNode; depth: number; collapse?: { mode: boolean; n: number } }) {
  const [collapsed, setCollapsed] = useState(false);
  // collapse-all / expand-all broadcast
  useEffect(() => {
    if (collapse && collapse.n > 0) setCollapsed(collapse.mode);
  }, [collapse]);
  return (
    <div className={depth > 0 ? 'ml-5 border-l border-zinc-800 pl-3 space-y-3' : 'space-y-3'}>
      <div className="relative">
        <DocCard doc={node.doc} />
        {node.children.length > 0 && (
          <button
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? `show ${node.children.length} sub-docs` : 'collapse sub-docs'}
            className="absolute right-3 bottom-2 text-xs text-zinc-600 hover:text-zinc-300 transition-colors"
          >
            {collapsed ? `▸ ${node.children.length} sub-doc${node.children.length > 1 ? 's' : ''}` : '▾'}
          </button>
        )}
      </div>
      {!collapsed && node.children.map((c) => <DocNode key={c.doc.id} node={c} depth={depth + 1} collapse={collapse} />)}
    </div>
  );
}

const KIND_STYLES: Record<string, string> = {
  rfc: 'bg-purple-500/10 border-purple-500/20 text-purple-400',
  runbook: 'bg-cyan-500/10 border-cyan-500/20 text-cyan-400',
  note: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  doc: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
};

const STATUS_STYLES: Record<string, string> = {
  active: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
  draft: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
  superseded: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  archived: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-500',
};

function DocCard({ doc }: { doc: Doc }) {
  const ticketLinks = (doc.links || []).filter(l => l.target_type === 'ticket');
  return (
    <a
      href={`/docs/${doc.id}`}
      className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/30 transition-colors"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${KIND_STYLES[doc.kind] || KIND_STYLES.doc}`}>
              {doc.kind}
            </span>
            <span className="text-sm font-medium text-zinc-200">{doc.title}</span>
            {doc.status !== 'active' && (
              <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${STATUS_STYLES[doc.status] || STATUS_STYLES.draft}`}>
                {doc.status}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-500 flex-wrap">
            <span className="font-mono text-zinc-600">{doc.slug}</span>
            <span>v{doc.current_rev}</span>
            <span>by {doc.source}</span>
            <span>{doc.group_scope ? `group:${doc.group_scope}` : 'commons'}</span>
            {ticketLinks.length > 0 && (
              <span className="text-zinc-400">↳ {ticketLinks.map(l => `#${l.target_id}`).join(' ')}</span>
            )}
          </div>
        </div>
        <span className="text-xs text-zinc-600 whitespace-nowrap shrink-0">{timeAgo(doc.updated_at)}</span>
      </div>
    </a>
  );
}
