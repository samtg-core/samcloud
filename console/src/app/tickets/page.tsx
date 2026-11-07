'use client';

import { timeAgo } from '@/lib/time';
import { Suspense, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import type { Ticket } from '@/lib/registry';
import { TicketFilters } from './TicketFilters';
import { CreateTicketForm } from './CreateTicketForm';
import { useScopeFilter } from '@/lib/scopeFilter';


export default function TicketsPage() {
  // useSearchParams() must sit under a Suspense boundary.
  return (
    <Suspense fallback={<div className="max-w-4xl mx-auto p-8 text-center text-zinc-600 text-sm">Loading tickets…</div>}>
      <TicketsInner />
    </Suspense>
  );
}

function TicketsInner() {
  const searchParams = useSearchParams();
  const filterStatus = searchParams.get('status') || undefined;
  const highlightParam = searchParams.get('highlight');
  const highlightId = highlightParam ? parseInt(highlightParam, 10) : null;

  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [loading, setLoading] = useState(true);
  // #327: as-you-type lookup + assignee filter. Client-side over the loaded
  // page — the whole open set is ~150 rows, no server round-trip warranted.
  const [q, setQ] = useState('');
  const [assignee, setAssignee] = useState(searchParams.get('assignee') || '');
  const [typeF, setTypeF] = useState('');
  const [priorityF, setPriorityF] = useState('');
  const [sortBy, setSortBy] = useState('created:desc');
  const scope = useScopeFilter();  // H2 (#380): global top-level scope narrow

  useEffect(() => {
    let alive = true;
    // Via the proxy (forwards the session token; registry scopes by
    // _can_see_ticket). /api/v1/tickets requires auth — no token = 401 = empty.
    async function load() {
      const qs = new URLSearchParams();
      if (filterStatus) qs.set('status', filterStatus);
      if (scope) qs.set('group_scope', scope);
      const path = '/api/v1/tickets' + (qs.toString() ? `?${qs.toString()}` : '');
      try {
        const res = await fetch(`/api/registry?path=${encodeURIComponent(path)}`, { cache: 'no-store' });
        const data = res.ok ? await res.json() : [];
        if (alive) setTickets(Array.isArray(data) ? data : []);
      } catch {
        if (alive) setTickets([]);
      } finally {
        if (alive) setLoading(false);
      }
    }
    setLoading(true);
    load();
    const id = setInterval(load, 15000);  // matches old revalidate=15
    return () => { alive = false; clearInterval(id); };
  }, [filterStatus, scope]);

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Tickets</h2>
          <p className="text-zinc-500 mt-1">Track issues across all devices and services</p>
        </div>
        <a href="/" className="text-sm text-zinc-400 hover:text-white transition-colors shrink-0">
          Dashboard
        </a>
      </div>

      {/* Filters */}
      <TicketFilters currentStatus={filterStatus} />

      {/* Search + assignee (#327) */}
      <div className="flex flex-col sm:flex-row gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search id, summary, body, anyone involved…"
          className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
        />
        <div className="grid grid-cols-2 sm:flex gap-2 min-w-0">
          <select
            value={assignee}
            onChange={(e) => setAssignee(e.target.value)}
            className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
          >
            <option value="">Any assignee</option>
            {[...new Set(tickets.map(t => t.assignee).filter(Boolean))].sort().map(a => (
              <option key={a as string} value={a as string}>{a}</option>
            ))}
          </select>
          <select value={typeF} onChange={(e) => setTypeF(e.target.value)}
                  className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none">
            <option value="">Any type</option>
            {['bug', 'feature', 'question', 'incident', 'task', 'chore', 'digest'].map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <select value={priorityF} onChange={(e) => setPriorityF(e.target.value)}
                  className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none">
            <option value="">Any priority</option>
            {['critical', 'high', 'medium', 'low'].map(pr => <option key={pr} value={pr}>{pr}</option>)}
          </select>
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}
                  className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none">
            <option value="created:desc">newest created</option>
            <option value="created:asc">oldest created</option>
            <option value="updated:desc">recently updated</option>
            <option value="updated:asc">least recently updated</option>
            <option value="priority">by priority</option>
          </select>
        </div>
      </div>

      {/* Create Ticket */}
      <CreateTicketForm />

      {/* Ticket List */}
      <div className="space-y-3">
        {loading ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">
            Loading…
          </div>
        ) : (() => {
          const needle = q.trim().toLowerCase();
          const PRIO = { critical: 0, high: 1, medium: 2, med: 2, low: 3 } as Record<string, number>;
          const shown = tickets.filter(t => {
            if (assignee && t.assignee !== assignee) return false;
            if (typeF && t.type !== typeF) return false;
            if (priorityF && (t.priority === 'med' ? 'medium' : t.priority) !== priorityF) return false;
            if (!needle) return true;
            return [`#${t.id}`, String(t.id), t.summary, t.body, t.source, t.assignee, t.device_id, t.service_id]
              .some(f => f && String(f).toLowerCase().includes(needle));
          }).sort((a, b) => {
            if (sortBy === 'priority') return (PRIO[a.priority] ?? 9) - (PRIO[b.priority] ?? 9) || b.id - a.id;
            const [field, dir] = sortBy.split(':');
            const ka = String((field === 'updated' ? a.updated_at : a.created_at) ?? '');
            const kb = String((field === 'updated' ? b.updated_at : b.created_at) ?? '');
            return dir === 'asc' ? ka.localeCompare(kb) : kb.localeCompare(ka);
          });
          return shown.length > 0 ? (
            shown.map((ticket) => (
              <TicketCard
                key={ticket.id}
                ticket={ticket}
                highlighted={ticket.id === highlightId}
              />
            ))
          ) : (
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">
              {needle || assignee ? 'Nothing matches the current search/filter' :
                filterStatus ? `No tickets with status "${filterStatus}"` : 'No tickets found'}
            </div>
          );
        })()}
      </div>
    </div>
  );
}

function TicketCard({ ticket, highlighted }: { ticket: Ticket; highlighted: boolean }) {
  const priorityColors: Record<string, string> = {
    critical: 'border-red-500/30 bg-red-500/5',
    high: 'border-orange-500/30 bg-orange-500/5',
    medium: 'border-amber-500/30 bg-amber-500/5',
    low: 'border-zinc-700 bg-zinc-900/50',
  };
  const priorityDots: Record<string, string> = {
    critical: 'bg-red-500',
    high: 'bg-orange-500',
    medium: 'bg-amber-500',
    low: 'bg-zinc-500',
  };
  const statusBadgeStyles: Record<string, string> = {
    open: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
    triaged: 'bg-purple-500/10 border-purple-500/20 text-purple-400',
    in_progress: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
    resolved: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    closed: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  };

  return (
    <a
      href={`/tickets/${ticket.id}`}
      className={`block rounded-lg border ${priorityColors[ticket.priority] || priorityColors.low} p-4 hover:bg-zinc-800/30 transition-colors cursor-pointer ${
        highlighted ? 'ring-2 ring-blue-500/50' : ''
      }`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 min-w-0">
          <span className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${priorityDots[ticket.priority] || 'bg-zinc-500'}`} />
          <div className="min-w-0 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-mono text-zinc-500">#{ticket.id}</span>
              <span className="text-sm font-medium text-zinc-200">{ticket.summary}</span>
              <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${
                statusBadgeStyles[ticket.status] || statusBadgeStyles.open
              }`}>
                {ticket.status.replace('_', ' ')}
              </span>
            </div>
            {ticket.body && (
              <p className="text-sm text-zinc-400 line-clamp-2">{ticket.body}</p>
            )}
            <div className="flex items-center gap-3 text-xs text-zinc-500">
              <span>{ticket.priority}</span>
              <span>{ticket.type}</span>
              <span>from {ticket.source}</span>
              {ticket.device_id && <span>{ticket.device_id}</span>}
              {ticket.service_id && <span>{ticket.service_id}</span>}
              {ticket.assignee && <span>assigned: {ticket.assignee}</span>}
            </div>
          </div>
        </div>
        <div className="text-right shrink-0">
          <span className="text-xs text-zinc-600 whitespace-nowrap">
            {timeAgo(ticket.created_at)}
          </span>
          {ticket.updated_at && ticket.updated_at !== ticket.created_at && (
            <div className="text-xs text-zinc-600 mt-1 whitespace-nowrap">upd {timeAgo(ticket.updated_at)}</div>
          )}
          {ticket.resolved_at && (
            <div className="text-xs text-emerald-600 mt-1">
              resolved {timeAgo(ticket.resolved_at)}
            </div>
          )}
        </div>
      </div>
    </a>
  );
}
