'use client';

// The inbox (#203 v2, doc #34): a stack of SAVED VIEWS — every section is a
// named filter over live data (owned rows, editable/deletable/reorderable),
// never a hardcoded block. Seeds arrive as data on first visit; "Blocked on
// you" et al are just views you can change. Notifications views keep the
// ack-as-handled machinery. Think Asana saved filters, plane-native.

import { timeAgo } from '@/lib/time';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useScopeGroups } from '@/lib/useScopeGroups';
import { Markdown } from '@/components/Markdown';
import { stashInboxQueue } from '@/components/InboxQueuePill';

type Entity = 'tickets' | 'tasks' | 'notifications' | 'docs';

interface ViewDef {
  id: number;
  owner: string;
  name: string;
  entity: Entity;
  filters: Record<string, unknown>;
  sort: string | null;
  group_by: string | null;
  limit_per_group: number | null;
  position: number | null;
}

interface ResultGroup { key: string; items: Record<string, unknown>[] }
interface Results { total: number; items?: Record<string, unknown>[]; groups?: ResultGroup[] }

interface HeaderStats {
  unread_total: number;
  moved_24h: { tickets: number; docs: number; tasks: number };
}

const WINDOWS = [
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
];

// Builder vocabulary — mirrors the registry whitelists (400s loud server-side).
// created_at / updated_at / id are the normalized trio every entity offers
// where the data supports it (id ~ creation order); entities add their own
// extras (priority, estimate_min…) on top.
const SORTS: Record<Entity, string[]> = {
  tickets: ['priority', 'created_at', 'updated_at', 'id'],
  tasks: ['estimate_min', 'created_at', 'updated_at', 'position', 'id'],
  notifications: ['id', 'created_at'],
  docs: ['created_at', 'updated_at', 'id'],
};
const SORT_LABELS: Record<string, string> = {
  created_at: 'created',
  updated_at: 'updated',
  id: 'id (creation order)',
};
const GROUPS: Record<Entity, string[]> = {
  tickets: ['priority', 'status', 'type'],
  tasks: ['project', 'status'],
  notifications: [],
  docs: ['kind', 'status'],
};
const STATUSES: Record<Entity, string[]> = {
  tickets: ['open', 'triaged', 'in_progress', 'resolved', 'closed'],
  tasks: ['todo', 'in_progress', 'blocked', 'done', 'cancelled'],
  notifications: [],
  docs: ['draft', 'active', 'superseded', 'archived'],
};
const PRIORITIES = ['low', 'med', 'high', 'critical'];
const TYPES = ['bug', 'feature', 'question', 'incident'];
const DOC_KINDS = ['doc', 'rfc', 'runbook', 'note'];

const ACCENT: Record<Entity, string> = {
  tickets: 'text-orange-400',
  tasks: 'text-purple-400',
  notifications: 'text-zinc-400',
  docs: 'text-blue-400',
};

async function reg(path: string, init?: RequestInit) {
  return fetch(`/api/registry?path=${encodeURIComponent(path)}`, {
    cache: 'no-store',
    ...(init || {}),
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  });
}


function chipText(k: string, v: unknown): string {
  const val = Array.isArray(v) ? v.join(', ') : String(v);
  return `${k}: ${val}`;
}

// ─── notification thread grouping (#268 — collapse per-thread, ack-on-open) ──
// One thread's N messages used to be N rows each needing its own "handled".
// We collapse by thread (ticket/doc/task), ack the whole thread at once, and
// surface @mentions individually: updates per thread, plus the ones you are
// tagged in. Acking per-thread also starves the cold-wake fuel (#266).

function notifTarget(n: Record<string, unknown>): { href: string; label: string; kind: string } {
  if (n.ticket_id) return { href: `/tickets/${n.ticket_id}`, label: `#${n.ticket_id}`, kind: 'ticket' };
  if (n.doc_id) return { href: `/docs/${n.doc_id}`, label: `doc ${n.doc_id}`, kind: 'doc' };
  if (n.task_id) return { href: `/tasks/${n.task_id}`, label: `task ${n.task_id}`, kind: 'task' };
  return { href: '#', label: '—', kind: 'misc' };
}

function threadKey(n: Record<string, unknown>): string {
  const t = notifTarget(n);
  return t.kind === 'misc' ? `misc:${String(n.id)}` : `${t.kind}:${t.href}`;
}

// A @mention is a direct tag at you — never collapse it into the thread digest.
function isMention(n: Record<string, unknown>): boolean {
  return String(n.event_type ?? '').endsWith('.mentioned');
}

function notifSnippet(n: Record<string, unknown>): string {
  // Body first (staging pass 2026-08-15): the preview should be the text
  // that TRIGGERED the row (the comment), not the parent's title/summary.
  const p = (n.payload || {}) as { summary?: string; title?: string; body?: string; note?: string; detail?: string };
  return p.body || p.note || p.detail || p.summary || p.title || '';
}

function eventLabel(n: Record<string, unknown>): string {
  return String(n.event_type ?? '').replace(/^(ticket|task|doc)\./, '');
}

// The parent's name (ticket summary / doc or task title) — shown alongside the
// triggering-text preview so a card answers both "what thread" and "what happened".
function parentTitle(n: Record<string, unknown>): string {
  const p = (n.payload || {}) as { summary?: string; title?: string };
  return p.summary || p.title || '';
}

// Card previews render markdown, soft-clamped
// by height since -webkit-line-clamp can't survive nested block tags.
function MdSnippet({ text, strong = false }: { text: string; strong?: boolean }) {
  if (!text) return null;
  return (
    <div className={`mt-1 max-h-11 overflow-hidden ${strong ? '' : 'opacity-80'} [&_*]:!text-xs [&_*]:!leading-snug [&_p]:!my-0.5 [&_pre]:!my-0.5 [&_ul]:!my-0.5 [&_ol]:!my-0.5 [&_img]:!max-h-8 [&_img]:!my-0`}>
      <Markdown muted={!strong} flatLinks>{text}</Markdown>
    </div>
  );
}

// A card's preview line: whatever last touched it (the latest comment), so a
// wall of cards reads as more than just titles. Falls back to the item's own
// body/detail when nothing's been commented on yet.
function rowPreview(it: Record<string, unknown>, fallbackKey?: string): string {
  const latest = it.latest_comment;
  if (typeof latest === 'string' && latest.trim()) return latest;
  const fallback = fallbackKey ? it[fallbackKey] : undefined;
  return typeof fallback === 'string' ? fallback : '';
}

// ─── per-entity result rows ──────────────────────────────────────────────────

function TicketRow({ t }: { t: Record<string, unknown> }) {
  const preview = rowPreview(t, 'body');
  return (
    <a href={`/tickets/${t.id}`} className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 hover:bg-zinc-800/30 transition-colors">
      <div className="flex items-center gap-2 text-sm flex-wrap">
        <span className="font-mono text-xs text-zinc-500">#{String(t.id)}</span>
        <span className="text-zinc-200">{String(t.summary ?? '')}</span>
      </div>
      <div className="text-xs text-zinc-500 mt-1">
        {String(t.priority ?? '')} · {String(t.status ?? '').replace('_', ' ')}
        {t.assignee ? ` · → ${String(t.assignee)}` : ''} · {timeAgo(String(t.created_at))}
      </div>
      <MdSnippet text={preview} />
    </a>
  );
}

const TASK_STATUSES = ['todo', 'in_progress', 'blocked', 'done', 'cancelled'];

function TaskRow({ t, onChanged }: { t: Record<string, unknown>; onChanged?: () => void }) {
  const preview = rowPreview(t, 'detail');
  const [menu, setMenu] = useState(false);
  const [busy, setBusy] = useState(false);
  async function setStatus(e: React.MouseEvent, status: string) {
    e.preventDefault(); e.stopPropagation();
    setBusy(true);
    try {
      await reg(`/api/v1/tasks/${t.id}`, { method: 'PATCH', body: JSON.stringify({ status }) });
      onChanged?.();
    } finally { setBusy(false); setMenu(false); }
  }
  return (
    <a href={`/tasks/${t.id}`} className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 hover:bg-zinc-800/30 transition-colors">
      <div className="text-sm text-zinc-200 flex items-center gap-2 flex-wrap">
        <span className="min-w-0">{String(t.title ?? '')}</span>
        {/* status is editable right on the card, not only in the task view */}
        <span className="relative inline-flex" onClick={(e) => { e.preventDefault(); e.stopPropagation(); }}>
          <button
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); setMenu((m) => !m); }}
            disabled={busy}
            className="rounded-full border border-zinc-700 px-1.5 py-0 text-[10px] text-zinc-400 hover:text-white disabled:opacity-50"
          >
            {busy ? '…' : String(t.status ?? 'todo').replace('_', ' ')} ▾
          </button>
          {menu && (
            <span className="absolute left-0 top-5 z-20 flex min-w-28 flex-col rounded-lg border border-zinc-700 bg-zinc-900 py-1 shadow-xl">
              {TASK_STATUSES.map((st) => (
                <button key={st} onClick={(e) => setStatus(e, st)}
                        className={`px-2.5 py-1 text-left text-[11px] hover:bg-zinc-800 ${st === t.status ? 'text-zinc-100 font-semibold' : 'text-zinc-400'}`}>
                  {st.replace('_', ' ')}
                </button>
              ))}
            </span>
          )}
        </span>
        <span className="text-xs text-zinc-500">
          {String(t.project ?? '')}
          {t.estimate_min ? ` · ~${String(t.estimate_min)}min` : ''}
        </span>
      </div>
      <MdSnippet text={preview} />
    </a>
  );
}

function DocRow({ d }: { d: Record<string, unknown> }) {
  const preview = rowPreview(d);
  return (
    <a href={`/docs/${d.id}`} className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 hover:bg-zinc-800/30 transition-colors">
      <div className="flex items-center gap-2 text-sm flex-wrap">
        <span className="text-zinc-200">{String(d.title ?? '')}</span>
      </div>
      <div className="text-xs text-zinc-500 mt-1">
        {String(d.kind ?? '')} · {String(d.status ?? '')}
        {d.source ? ` · by ${String(d.source)}` : ''} · {timeAgo(String(d.updated_at ?? d.created_at))}
      </div>
      <MdSnippet text={preview} />
    </a>
  );
}

// Fire-and-forget ack that survives the navigation of clicking through to the
// thread — "reading the thread clears it" (#268). keepalive lets the POST
// complete after the <a> navigates away.
function ackOnOpen(ids: number[]) {
  if (!ids.length) return;
  reg('/api/v1/notifications/ack', {
    method: 'POST',
    body: JSON.stringify({ ids, surface: 'console' }),
    keepalive: true,
  }).catch(() => {});
}

// A single @mention — direct tag at you, shown on its own (never collapsed).
function NotifMentionRow({ n, onAck, onOpen }: { n: Record<string, unknown>; onAck: (ids: number[]) => Promise<void>; onOpen?: () => void }) {
  const [busy, setBusy] = useState(false);
  const target = notifTarget(n);
  const snippet = notifSnippet(n);
  const unread = !n.read;
  return (
    <div className="flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
      <div className="min-w-0 flex-1">
        <a href={target.href} onClick={() => { ackOnOpen(unread ? [Number(n.id)] : []); onOpen?.(); }} className="block group">
          <div className="flex items-center gap-2 flex-wrap text-sm">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-400 border border-amber-500/40 rounded px-1">@you</span>
            <span className="text-xs font-mono text-zinc-500">{target.label}</span>
            {parentTitle(n) && <span className="text-xs text-zinc-300 truncate max-w-60">{parentTitle(n)}</span>}
            <span className="text-xs text-zinc-600">by {String(n.actor ?? '')}</span>
            <span className="text-xs text-zinc-600">{timeAgo(String(n.created_at))}</span>
            {!unread && <span className="text-xs text-zinc-700">read</span>}
          </div>
          <MdSnippet text={snippet} strong />
        </a>
      </div>
      {unread && (
        <button
          onClick={async () => { setBusy(true); await onAck([Number(n.id)]); setBusy(false); }}
          disabled={busy}
          className="text-xs text-zinc-500 hover:text-emerald-400 border border-zinc-700 rounded px-2 py-1 shrink-0 transition-colors disabled:opacity-40"
          title="Ack = handled. It will not come back."
        >
          {busy ? '…' : 'handled'}
        </button>
      )}
    </div>
  );
}

// A thread's updates, collapsed into one card (#268). Newest on top, expandable
// to the full run; a single "Handle" acks every unread row in the thread.
function NotifThreadCard({ rows, onAck, onOpen }: { rows: Record<string, unknown>[]; onAck: (ids: number[]) => Promise<void>; onOpen?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const target = notifTarget(rows[0]);
  const latest = rows[0];
  const unreadIds = rows.filter((r) => !r.read).map((r) => Number(r.id));
  const snippet = notifSnippet(latest);
  const total = rows.length;
  return (
    <div className={`rounded-lg border p-3 ${unreadIds.length ? 'border-zinc-700 bg-zinc-900/60' : 'border-zinc-800 bg-zinc-900/40'}`}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <a href={target.href} onClick={() => { ackOnOpen(unreadIds); onOpen?.(); }} className="block group">
            <div className="flex items-center gap-2 flex-wrap text-sm">
              <span className="text-xs font-mono text-zinc-500">{target.label}</span>
              {parentTitle(latest) && <span className="text-xs text-zinc-300 truncate max-w-60">{parentTitle(latest)}</span>}
              <span className="text-xs text-zinc-400">
                {total} update{total > 1 ? 's' : ''}
                {unreadIds.length > 0 && unreadIds.length < total && <span className="text-emerald-400"> · {unreadIds.length} new</span>}
              </span>
              <span className="text-xs text-zinc-600">latest {eventLabel(latest)} by {String(latest.actor ?? '')}</span>
              <span className="text-xs text-zinc-600">{timeAgo(String(latest.created_at))}</span>
            </div>
            <MdSnippet text={snippet} />
          </a>
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          {unreadIds.length > 0 && (
            <button
              onClick={async () => { setBusy(true); await onAck(unreadIds); setBusy(false); }}
              disabled={busy}
              className="text-xs text-zinc-500 hover:text-emerald-400 border border-zinc-700 rounded px-2 py-1 transition-colors disabled:opacity-40"
              title="Ack every update in this thread. They will not come back."
            >
              {busy ? '…' : `Handle ${unreadIds.length}`}
            </button>
          )}
          {total > 1 && (
            <button onClick={() => setExpanded((e) => !e)} className="text-xs text-zinc-600 hover:text-white px-1">
              {expanded ? 'hide' : `show ${total}`}
            </button>
          )}
        </div>
      </div>
      {expanded && total > 1 && (
        <div className="mt-2 space-y-1 border-t border-zinc-800 pt-2">
          {rows.map((r) => <ExpandedRow key={String(r.id)} r={r} />)}
        </div>
      )}
    </div>
  );
}

// One update in an expanded card. The snippet wraps under its header, three
// lines at most; a tap shows all of it. (#1082: truncating it to one line left
// no way to read the rest of the row on a phone.)
function ExpandedRow({ r }: { r: Record<string, unknown> }) {
  const [full, setFull] = useState(false);
  const text = notifSnippet(r);
  return (
    <div className="min-w-0 text-xs">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className={r.read ? 'text-zinc-700' : 'text-emerald-500'}>{r.read ? '·' : '•'}</span>
        <span className="text-zinc-500">{eventLabel(r)} by {String(r.actor ?? '')}</span>
        <span className="text-zinc-700">{timeAgo(String(r.created_at))}</span>
      </div>
      {text && (
        <button
          type="button"
          onClick={() => setFull((f) => !f)}
          title={full ? 'Show less' : 'Show all of it'}
          className={`block w-full pl-3 text-left whitespace-pre-line text-zinc-400 [overflow-wrap:anywhere] ${full ? '' : 'line-clamp-3'}`}
        >
          {text}
        </button>
      )}
    </div>
  );
}

// ─── view builder (create + edit share it) ───────────────────────────────────

interface BuilderState {
  name: string;
  entity: Entity;
  assignee: string;
  status: string[];
  priority: string[];
  type: string[];
  kind: string[];
  tag: string;
  project: string;
  group_scope: string;
  age_hours: string;
  unread: boolean;
  sort: string;
  group_by: string;
  limit_per_group: string;
}

function builderFromView(v?: ViewDef): BuilderState {
  const f = (v?.filters || {}) as Record<string, unknown>;
  const arr = (x: unknown) => (Array.isArray(x) ? x.map(String) : x ? [String(x)] : []);
  return {
    name: v?.name || '',
    entity: v?.entity || 'tickets',
    assignee: f.assignee ? String(f.assignee) : '',
    status: arr(f.status),
    priority: arr(f.priority),
    type: arr(f.type),
    kind: arr(f.kind),
    tag: arr(f.tag).join(', '),
    project: f.project ? String(f.project) : '',
    group_scope: f.group_scope ? String(f.group_scope) : '',
    age_hours: f.age_hours ? String(f.age_hours) : '',
    unread: !!f.unread,
    sort: v?.sort || '',
    group_by: v?.group_by || '',
    limit_per_group: v?.limit_per_group ? String(v.limit_per_group) : '',
  };
}

function builderToPayload(b: BuilderState, editing: boolean) {
  const filters: Record<string, unknown> = {};
  if (b.assignee.trim()) filters.assignee = b.assignee.trim();
  if (b.status.length) filters.status = b.status;
  if (b.priority.length && b.entity !== 'notifications' && b.entity !== 'docs') filters.priority = b.priority;
  if (b.type.length && b.entity === 'tickets') filters.type = b.type;
  if (b.kind.length && b.entity === 'docs') filters.kind = b.kind;
  const tags = b.tag.split(',').map((s) => s.trim()).filter(Boolean);
  if (tags.length && b.entity !== 'notifications' && b.entity !== 'docs') filters.tag = tags;
  if (b.project.trim() && b.entity === 'tasks') filters.project = b.project.trim();
  if (b.group_scope && b.entity !== 'notifications') filters.group_scope = b.group_scope;
  if (b.age_hours.trim()) filters.age_hours = Number(b.age_hours);
  if (b.unread && b.entity === 'notifications') filters.unread = true;
  const payload: Record<string, unknown> = {
    name: b.name.trim(),
    filters,
    sort: b.sort || null,
    group_by: b.group_by || null,
    limit_per_group: b.limit_per_group ? Number(b.limit_per_group) : null,
  };
  if (!editing) payload.entity = b.entity;
  return payload;
}

function MultiPick({ options, value, onChange }: { options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="flex flex-wrap gap-1">
      {options.map((o) => {
        const on = value.includes(o);
        return (
          <button
            key={o}
            type="button"
            onClick={() => onChange(on ? value.filter((x) => x !== o) : [...value, o])}
            className={`px-2 py-0.5 rounded text-xs border transition-colors ${on ? 'bg-zinc-700 border-zinc-600 text-white' : 'border-zinc-800 text-zinc-500 hover:text-white'}`}
          >
            {o.replace('_', ' ')}
          </button>
        );
      })}
    </div>
  );
}

function ViewBuilder({ view, onDone, onCancel }: { view?: ViewDef; onDone: () => void; onCancel: () => void }) {
  const [b, setB] = useState<BuilderState>(() => builderFromView(view));
  const scopeGroups = useScopeGroups();
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);
  const editing = !!view;
  const set = (patch: Partial<BuilderState>) => setB((s) => ({ ...s, ...patch }));

  async function save() {
    if (!b.name.trim()) { setErr('name required'); return; }
    setSaving(true);
    setErr('');
    const res = view
      ? await reg(`/api/v1/views/${view.id}`, { method: 'PATCH', body: JSON.stringify(builderToPayload(b, true)) })
      : await reg('/api/v1/views', { method: 'POST', body: JSON.stringify(builderToPayload(b, false)) });
    setSaving(false);
    if (res.ok) { onDone(); return; }
    const data = await res.json().catch(() => ({}));
    setErr(String(data.detail || `save failed (${res.status})`));
  }

  const field = 'space-y-1';
  const label = 'text-xs uppercase tracking-wide text-zinc-500';
  const input = 'w-full bg-zinc-900 border border-zinc-800 rounded-md px-2 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-600';

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-900/80 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold text-zinc-200">{editing ? `Edit view · ${view!.name}` : 'New view'}</h4>
        <button onClick={onCancel} className="text-xs text-zinc-500 hover:text-white">cancel</button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className={field}>
          <div className={label}>name</div>
          <input className={input} value={b.name} onChange={(e) => set({ name: e.target.value })} placeholder="e.g. High-priority open bugs" />
        </div>
        <div className={field}>
          <div className={label}>entity {editing && <span className="text-zinc-700">(fixed)</span>}</div>
          <select
            className={input}
            value={b.entity}
            disabled={editing}
            onChange={(e) => set({ entity: e.target.value as Entity, status: [], priority: [], type: [], kind: [], group_by: '', sort: '' })}
          >
            <option value="tickets">tickets</option>
            <option value="tasks">tasks</option>
            <option value="notifications">notifications</option>
            <option value="docs">docs</option>
          </select>
        </div>
        <div className={field}>
          <div className={label}>{b.entity === 'docs' ? 'author' : 'assignee'} <span className="text-zinc-700">(:me = you)</span></div>
          <input className={input} value={b.assignee} onChange={(e) => set({ assignee: e.target.value })} placeholder=":me" disabled={b.entity === 'notifications'} />
        </div>
        <div className={field}>
          <div className={label}>age (hours)</div>
          <input className={input} value={b.age_hours} onChange={(e) => set({ age_hours: e.target.value.replace(/[^0-9]/g, '') })} placeholder="e.g. 168" />
        </div>
        {STATUSES[b.entity].length > 0 && (
          <div className={`${field} sm:col-span-2`}>
            <div className={label}>status</div>
            <MultiPick options={STATUSES[b.entity]} value={b.status} onChange={(v) => set({ status: v })} />
          </div>
        )}
        {b.entity !== 'notifications' && b.entity !== 'docs' && (
          <div className={field}>
            <div className={label}>priority</div>
            <MultiPick options={PRIORITIES} value={b.priority} onChange={(v) => set({ priority: v })} />
          </div>
        )}
        {b.entity === 'tickets' && (
          <div className={field}>
            <div className={label}>type</div>
            <MultiPick options={TYPES} value={b.type} onChange={(v) => set({ type: v })} />
          </div>
        )}
        {b.entity === 'docs' && (
          <div className={field}>
            <div className={label}>kind</div>
            <MultiPick options={DOC_KINDS} value={b.kind} onChange={(v) => set({ kind: v })} />
          </div>
        )}
        {b.entity !== 'notifications' && b.entity !== 'docs' && (
          <div className={field}>
            <div className={label}>tags <span className="text-zinc-700">(comma-sep)</span></div>
            <input className={input} value={b.tag} onChange={(e) => set({ tag: e.target.value })} placeholder="decision, tapes" />
          </div>
        )}
        {b.entity === 'tasks' && (
          <div className={field}>
            <div className={label}>project <span className="text-zinc-700">(slug)</span></div>
            <input className={input} value={b.project} onChange={(e) => set({ project: e.target.value })} placeholder="tapes" />
          </div>
        )}
        {b.entity !== 'notifications' && (
          <div className={field}>
            <div className={label}>scope <span className="text-zinc-700">(group)</span></div>
            <select className={input} value={b.group_scope} onChange={(e) => set({ group_scope: e.target.value })}>
              <option value="">any</option>
              {scopeGroups.map((g) => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
          </div>
        )}
        {b.entity === 'notifications' && (
          <div className={field}>
            <div className={label}>unread only</div>
            <button
              type="button"
              onClick={() => set({ unread: !b.unread })}
              className={`px-2 py-1 rounded text-xs border ${b.unread ? 'bg-zinc-700 border-zinc-600 text-white' : 'border-zinc-800 text-zinc-500'}`}
            >
              {b.unread ? 'yes' : 'no'}
            </button>
          </div>
        )}
        <div className={field}>
          <div className={label}>sort</div>
          <select className={input} value={b.sort} onChange={(e) => set({ sort: e.target.value })}>
            <option value="">default (id desc)</option>
            {SORTS[b.entity].map((s) => (
              <option key={s} value={s}>{SORT_LABELS[s] || s}</option>
            ))}
            {SORTS[b.entity].map((s) => (
              <option key={`${s}:desc`} value={`${s}:desc`}>{SORT_LABELS[s] || s} desc</option>
            ))}
          </select>
        </div>
        {GROUPS[b.entity].length > 0 && (
          <div className={field}>
            <div className={label}>group by</div>
            <select className={input} value={b.group_by} onChange={(e) => set({ group_by: e.target.value })}>
              <option value="">no grouping</option>
              {GROUPS[b.entity].map((g) => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
          </div>
        )}
        {b.group_by && (
          <div className={field}>
            <div className={label}>per group limit</div>
            <input className={input} value={b.limit_per_group} onChange={(e) => set({ limit_per_group: e.target.value.replace(/[^0-9]/g, '') })} placeholder="1" />
          </div>
        )}
      </div>
      {err && <p className="text-xs text-red-400">{err}</p>}
      <button
        onClick={save}
        disabled={saving}
        className="text-sm border border-zinc-600 rounded-md px-3 py-1.5 text-zinc-200 hover:bg-zinc-800 transition-colors disabled:opacity-40"
      >
        {saving ? 'saving…' : editing ? 'Save changes' : 'Create view'}
      </button>
    </div>
  );
}

// ─── one view = one section ──────────────────────────────────────────────────

function ViewSection({ view, first, last, refreshTick, onChanged, onMove, onDragTo, index }: {
  view: ViewDef;
  first: boolean;
  last: boolean;
  refreshTick: number;
  onChanged: () => void;
  onMove: (dir: -1 | 1) => void;
  onDragTo: (from: number, to: number) => void;
  index: number;
}) {
  const [results, setResults] = useState<Results | null>(null);
  const [editing, setEditing] = useState(false);
  // Collapse survives reloads per view.
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    try { setCollapsed(localStorage.getItem(`samcloud-view-collapsed:${view.id}`) === '1'); } catch { /* ignore */ }
  }, [view.id]);
  function toggleCollapsed() {
    setCollapsed((c) => {
      try { localStorage.setItem(`samcloud-view-collapsed:${view.id}`, c ? '0' : '1'); } catch { /* ignore */ }
      return !c;
    });
  }
  const [dragOver, setDragOver] = useState(false);

  const load = useCallback(async () => {
    const res = await reg(`/api/v1/views/${view.id}/results?limit=30`);
    if (res.ok) setResults(await res.json());
  }, [view.id]);

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load, refreshTick]);

  const ackIds = useCallback(async (ids: number[]) => {
    if (!ids.length) return;
    await reg('/api/v1/notifications/ack', { method: 'POST', body: JSON.stringify({ ids, surface: 'console' }) });
    await load();
    onChanged();
  }, [load, onChanged]);

  async function remove() {
    if (!confirm(`Delete view "${view.name}"? The underlying data is untouched.`)) return;
    await reg(`/api/v1/views/${view.id}`, { method: 'DELETE' });
    onChanged();
  }

  const renderItem = (it: Record<string, unknown>) =>
    view.entity === 'tickets' ? <TicketRow key={String(it.id)} t={it} />
    : view.entity === 'docs' ? <DocRow key={String(it.id)} d={it} />
    : <TaskRow key={String(it.id)} t={it} onChanged={load} />;

  // Notifications collapse per thread (#268): @mentions stay individual, every
  // other thread's run folds into one card whose single Handle acks the lot.
  function renderNotifications(items: Record<string, unknown>[]) {
    const mentions = items.filter(isMention);
    const groups = new Map<string, Record<string, unknown>[]>();
    for (const n of items.filter((x) => !isMention(x))) {
      const k = threadKey(n);
      const arr = groups.get(k);
      if (arr) arr.push(n); else groups.set(k, [n]);
    }
    // #391: opening any card queues the REST of the list for the floating
    // next-pill — reading N notifications is N clicks, not N round-trips.
    const entries = [...groups.entries()];
    const queueItems = [
      ...mentions.map((n) => ({ href: notifTarget(n).href, label: `${notifTarget(n).label} ${notifSnippet(n).slice(0, 40)}`.trim() })),
      ...entries.map(([, rows]) => ({ href: notifTarget(rows[0]).href, label: `${notifTarget(rows[0]).label} ${notifSnippet(rows[0]).slice(0, 40)}`.trim() })),
    ];
    const stashFor = (href: string) => () =>
      stashInboxQueue(queueItems.filter((it) => it.href !== href));
    return (
      <>
        {mentions.map((n) => <NotifMentionRow key={String(n.id)} n={n} onAck={ackIds} onOpen={stashFor(notifTarget(n).href)} />)}
        {entries.map(([k, rows]) => <NotifThreadCard key={k} rows={rows} onAck={ackIds} onOpen={stashFor(notifTarget(rows[0]).href)} />)}
      </>
    );
  }

  const total = results?.total ?? 0;
  const chips: string[] = Object.entries(view.filters || {}).map(([k, v]) => chipText(k, v));

  return (
    <section className="space-y-3">
      <div
        className={`flex items-center gap-2 flex-wrap rounded-md -mx-1 px-1 ${dragOver ? 'bg-zinc-800/40 ring-1 ring-zinc-600' : ''}`}
        draggable
        onDragStart={(e) => e.dataTransfer.setData('text/x-view-index', String(index))}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault(); setDragOver(false);
          const from = Number(e.dataTransfer.getData('text/x-view-index'));
          if (!Number.isNaN(from) && from !== index) onDragTo(from, index);
        }}
      >
        <button onClick={toggleCollapsed} title={collapsed ? 'expand' : 'minimise'} className="text-xs text-zinc-600 hover:text-white cursor-pointer">
          {collapsed ? '▸' : '▾'}
        </button>
        <h3 className={`text-sm font-semibold uppercase tracking-wide cursor-grab active:cursor-grabbing ${ACCENT[view.entity]}`}>
          {view.name} ({total})
        </h3>
        <span className="text-xs text-zinc-700">{view.entity}</span>
        {chips.map((c) => (
          <span key={c} className="text-xs px-1.5 py-0.5 rounded border border-zinc-800 text-zinc-500">{c}</span>
        ))}
        {view.sort && <span className="text-xs px-1.5 py-0.5 rounded border border-zinc-800/60 text-zinc-600">sort {view.sort}</span>}
        {view.group_by && <span className="text-xs px-1.5 py-0.5 rounded border border-zinc-800/60 text-zinc-600">by {view.group_by}{view.limit_per_group ? ` ×${view.limit_per_group}` : ''}</span>}
        <span className="flex-1" />
        <span className="flex items-center gap-1 text-xs text-zinc-600">
          {!first && <button onClick={() => onMove(-1)} className="hover:text-white px-1" title="move up">↑</button>}
          {!last && <button onClick={() => onMove(1)} className="hover:text-white px-1" title="move down">↓</button>}
          <button onClick={() => setEditing((e) => !e)} className="hover:text-white px-1" title="edit view">edit</button>
          <button onClick={remove} className="hover:text-red-400 px-1" title="delete view">×</button>
        </span>
      </div>

      {editing && (
        <ViewBuilder
          view={view}
          onDone={() => { setEditing(false); onChanged(); load(); }}
          onCancel={() => setEditing(false)}
        />
      )}

      {collapsed ? null : !results ? (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-center text-zinc-700 text-xs">loading…</div>
      ) : results.groups ? (
        results.groups.length === 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-center text-zinc-600 text-sm">Nothing matches.</div>
        ) : (
          results.groups.map((g) => (
            <div key={g.key} className="space-y-2">
              <div className="text-xs text-zinc-500 font-mono">{g.key}</div>
              {g.items.map(renderItem)}
            </div>
          ))
        )
      ) : (results.items || []).length === 0 ? (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-center text-zinc-600 text-sm">Nothing matches.</div>
      ) : view.entity === 'notifications' ? (
        renderNotifications(results.items || [])
      ) : (
        (results.items || []).map(renderItem)
      )}
      {!collapsed && total > 30 && <p className="text-xs text-zinc-600">…{total - 30} more not shown.</p>}
    </section>
  );
}

// ─── page ────────────────────────────────────────────────────────────────────

export default function InboxPage() {
  const [views, setViews] = useState<ViewDef[] | null>(null);
  const [stats, setStats] = useState<HeaderStats | null>(null);
  const [windowHours, setWindowHours] = useState(24);
  const [adding, setAdding] = useState(false);
  const [tick, setTick] = useState(0);
  const loadingRef = useRef(false);

  const loadViews = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    try {
      const res = await reg('/api/v1/views');
      if (res.ok) {
        const data = await res.json();
        const vs = (data.views || []) as ViewDef[];
        vs.sort((a, b) => (a.position ?? 0) - (b.position ?? 0) || a.id - b.id);
        setViews(vs);
      }
    } finally {
      loadingRef.current = false;
    }
  }, []);

  const loadStats = useCallback(async () => {
    const res = await reg(`/api/v1/users/me/briefing?window_hours=${windowHours}`);
    if (res.ok) {
      const b = await res.json();
      setStats({ unread_total: b.unread_total ?? 0, moved_24h: b.moved_24h ?? { tickets: 0, docs: 0, tasks: 0 } });
    }
  }, [windowHours]);

  useEffect(() => { loadViews(); }, [loadViews]);
  useEffect(() => {
    loadStats();
    const id = setInterval(loadStats, 30000);
    return () => clearInterval(id);
  }, [loadStats]);

  const refreshAll = useCallback(() => { loadViews(); loadStats(); setTick((t) => t + 1); }, [loadViews, loadStats]);

  async function ackAll() {
    await reg('/api/v1/notifications/ack', { method: 'POST', body: JSON.stringify({ all: true, surface: 'console' }) });
    refreshAll();
  }

  async function move(idx: number, dir: -1 | 1) {
    if (!views) return;
    const a = views[idx];
    const b = views[idx + dir];
    if (!a || !b) return;
    const pa = a.position ?? idx;
    const pb = b.position ?? idx + dir;
    await Promise.all([
      reg(`/api/v1/views/${a.id}`, { method: 'PATCH', body: JSON.stringify({ position: pb }) }),
      reg(`/api/v1/views/${b.id}`, { method: 'PATCH', body: JSON.stringify({ position: pa }) }),
    ]);
    loadViews();
  }

  // Drag a section header onto another to reorder.
  // Positions are rewritten sequentially for every view so the order is total.
  async function dragTo(from: number, to: number) {
    if (!views) return;
    const order = [...views];
    const [moved] = order.splice(from, 1);
    order.splice(to, 0, moved);
    await Promise.all(order.map((v, i) =>
      (v.position ?? -1) === i ? Promise.resolve(null)
        : reg(`/api/v1/views/${v.id}`, { method: 'PATCH', body: JSON.stringify({ position: i }) })));
    loadViews();
  }

  if (views === null) {
    return <div className="max-w-4xl mx-auto p-8 text-center text-zinc-600 text-sm">Loading your views…</div>;
  }

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Inbox</h2>
          <p className="text-zinc-500 mt-1 flex items-center gap-2 flex-wrap text-sm">
            <span>{stats?.unread_total ?? '…'} unread</span>
            <span>· last</span>
            <span className="inline-flex rounded-md border border-zinc-800 overflow-hidden">
              {WINDOWS.map((w) => (
                <button
                  key={w.hours}
                  onClick={() => setWindowHours(w.hours)}
                  className={`px-2 py-0.5 text-xs ${windowHours === w.hours ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-white'}`}
                >
                  {w.label}
                </button>
              ))}
            </span>
            {stats && <span>: {stats.moved_24h.tickets} tickets, {stats.moved_24h.docs} docs, {stats.moved_24h.tasks} tasks moved</span>}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {(stats?.unread_total ?? 0) > 0 && (
            <button onClick={ackAll} className="text-sm text-zinc-400 hover:text-white border border-zinc-700 rounded-md px-3 py-1.5 transition-colors">
              Mark all handled
            </button>
          )}
          <button onClick={() => setAdding((a) => !a)} className="text-sm text-zinc-300 hover:text-white border border-zinc-600 rounded-md px-3 py-1.5 transition-colors">
            {adding ? 'Close' : '+ Add view'}
          </button>
        </div>
      </div>

      {adding && <ViewBuilder onDone={() => { setAdding(false); refreshAll(); }} onCancel={() => setAdding(false)} />}

      {views.length === 0 && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">
          No views yet — add one above. (First visit normally seeds four starters.)
        </div>
      )}

      {views.map((v, i) => (
        <ViewSection
          key={v.id}
          view={v}
          index={i}
          first={i === 0}
          last={i === views.length - 1}
          refreshTick={tick}
          onChanged={refreshAll}
          onMove={(dir) => move(i, dir)}
          onDragTo={dragTo}
        />
      ))}
    </div>
  );
}
