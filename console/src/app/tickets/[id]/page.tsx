'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { useEvents } from '@/hooks/useEvents';
import { Markdown } from '@/components/Markdown';
import { fmtLocal } from '@/lib/time';
import { AttachButton } from '@/components/AttachButton';
import { useScopeGroups } from '@/lib/useScopeGroups';

/** "active now" under 3 minutes; otherwise a compact age. */
function presenceLabel(lastSeen: string | null | undefined): { text: string; live: boolean } | null {
  if (!lastSeen) return null;
  const min = Math.floor((Date.now() - new Date(lastSeen + (lastSeen.endsWith('Z') ? '' : 'Z')).getTime()) / 60000);
  if (isNaN(min)) return null;
  if (min < 3) return { text: 'active now', live: true };
  if (min < 60) return { text: `seen ${min}m ago`, live: false };
  if (min < 60 * 24) return { text: `seen ${Math.floor(min / 60)}h ago`, live: false };
  return { text: `seen ${Math.floor(min / 1440)}d ago`, live: false };
}

interface Ticket {
  id: number;
  source: string;
  type: string;
  priority: string;
  status: string;
  summary: string;
  body: string | null;
  assignee: string | null;
  group_scope: string | null;
  created_at: string;
  updated_at: string;
  comments: Comment[];
}

interface Comment {
  id: number;
  author: string;
  body: string;
  created_at: string;
  session_fp?: string | null;        // #388: which surface of the identity wrote it
  home_thread_id?: number | null;
  parent_comment_id?: number | null; // #383: one-level reply threading
}

const PROXY = '/api/registry';

const priorityStyles: Record<string, string> = {
  critical: 'bg-red-500/10 border-red-500/20 text-red-400',
  high: 'bg-orange-500/10 border-orange-500/20 text-orange-400',
  medium: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
  low: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
};

const statusStyles: Record<string, string> = {
  open: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
  resolved: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
  closed: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
};

export default function TicketDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [commentBody, setCommentBody] = useState('');
  const [replyTo, setReplyTo] = useState<Comment | null>(null);
  const [commenting, setCommenting] = useState(false);
  const [commentError, setCommentError] = useState('');
  const [ticketId, setTicketId] = useState<string>('');
  const [users, setUsers] = useState<string[]>([]);
  const scopeGroups = useScopeGroups();  // #330: groups this user can scope to
  const [lastSeenBy, setLastSeenBy] = useState<Record<string, string>>({});
  const [actionError, setActionError] = useState('');
  const [firstNewId, setFirstNewId] = useState<number | null>(null);
  const jumped = useRef(false);
  const { subscribe, connected } = useEvents();

  const loadTicket = useCallback(async () => {
    if (!ticketId) return;
    try {
      const res = await fetch(`${PROXY}?path=/api/v1/tickets/${ticketId}`);
      if (res.ok) {
        setTicket(await res.json());
      } else {
        const d = await res.json().catch(() => ({}));
        setError(d.detail || 'Could not load ticket');
      }
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, [ticketId]);

  useEffect(() => {
    params.then(({ id }) => setTicketId(id));
  }, [params]);

  useEffect(() => { if (ticketId) loadTicket(); }, [ticketId, loadTicket]);

  // Assignee options (#171) + last_seen for the presence badge (#327 —
  // "shows it's picked up": last_seen stamps on every inbox read, so it's a
  // faithful is-anyone-listening signal).
  useEffect(() => {
    fetch(`${PROXY}?path=/api/v1/users`)
      .then(res => (res.ok ? res.json() : []))
      .then((d) => {
        if (!Array.isArray(d)) return;
        setUsers(d.map((u: { username: string }) => u.username));
        setLastSeenBy(Object.fromEntries(d.map((u: { username: string; last_seen?: string }) => [u.username, u.last_seen || ''])));
      })
      .catch(() => {});
  }, []);

  // Jump to the earliest unseen comment (#327 "avoid scroll"): a per-browser
  // read marker in localStorage — no backend read-state needed for v1.
  useEffect(() => {
    if (!ticket || jumped.current) return;
    jumped.current = true;
    const key = `ticket-seen-${ticket.id}`;
    const seen = parseInt(localStorage.getItem(key) || '0', 10);
    const maxId = ticket.comments.reduce((m, c) => Math.max(m, c.id), 0);
    const firstNew = ticket.comments.find(c => c.id > seen);
    if (seen > 0 && firstNew) {
      setFirstNewId(firstNew.id);
      setTimeout(() => document.getElementById(`comment-${firstNew.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 150);
    }
    localStorage.setItem(key, String(maxId));
  }, [ticket]);

  // Live updates
  useEffect(() => {
    const unsub = subscribe('ticket.*', (e) => {
      if (e.data?.id?.toString() === ticketId) loadTicket();
    });
    return unsub;
  }, [subscribe, ticketId, loadTicket]);

  async function handleComment(e: React.FormEvent) {
    e.preventDefault();
    if (!commentBody.trim()) return;
    setCommenting(true);
    setCommentError('');
    try {
      // `author` is bearer-derived by the registry (extra fields forbidden →
      // 422); the proxy forwards the session token, so the comment is
      // attributed to the logged-in user. Sending author here used to 422 and
      // fail silently.
      const res = await fetch(`${PROXY}?path=/api/v1/tickets/${ticketId}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: commentBody, ...(replyTo ? { parent_comment_id: replyTo.id } : {}) }),
      });
      if (res.ok) {
        setCommentBody('');
        setReplyTo(null);
        loadTicket();
      } else {
        const d = await res.json().catch(() => ({}));
        setCommentError(d.detail || `Could not add comment (${res.status})`);
      }
    } catch {
      setCommentError('Could not connect to registry');
    }
    setCommenting(false);
  }

  async function patchTicket(patch: Record<string, string>) {
    setActionError('');
    try {
      const res = await fetch(`${PROXY}?path=/api/v1/tickets/${ticketId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setActionError(d.detail || `Could not update ticket (${res.status})`);
      }
    } catch {
      setActionError('Could not connect to registry');
    }
    loadTicket();
  }

  if (loading) return <div className="max-w-4xl mx-auto"><p className="text-zinc-500">Loading...</p></div>;
  if (error) return <div className="max-w-4xl mx-auto"><div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6"><p className="text-red-400">{error}</p></div></div>;
  if (!ticket) return null;

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center gap-3 text-sm">
        <a href="/tickets" className="text-zinc-500 hover:text-zinc-300">Tickets</a>
        <span className="text-zinc-700">/</span>
        <span className="text-zinc-400">#{ticket.id}</span>
        {connected && <span className="inline-flex items-center gap-1 text-xs text-emerald-500 ml-auto"><span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />live</span>}
      </div>

      {/* Header */}
      <div>
        <h2 className="text-xl font-bold text-zinc-100">{ticket.summary}</h2>
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${statusStyles[ticket.status] || statusStyles.open}`}>{ticket.status}</span>
          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${priorityStyles[ticket.priority] || priorityStyles.medium}`}>{ticket.priority}</span>
          <span className="text-xs text-zinc-500">{ticket.type}</span>
          <span className="text-xs text-zinc-500">by {ticket.source}</span>
          {ticket.assignee && <span className="text-xs text-zinc-500">assigned to <span className="text-zinc-300">{ticket.assignee}</span></span>}
          {ticket.assignee && (() => {
            const p = presenceLabel(lastSeenBy[ticket.assignee]);
            return p ? (
              <span className={`inline-flex items-center gap-1 text-xs ${p.live ? 'text-emerald-400' : 'text-zinc-600'}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${p.live ? 'bg-emerald-500 animate-pulse' : 'bg-zinc-600'}`} />
                {p.text}
              </span>
            ) : null;
          })()}
        </div>
      </div>

      {/* Body */}
      {ticket.body && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-5">
          <Markdown>{ticket.body}</Markdown>
        </div>
      )}

      {/* Status + assignee actions */}
      <div className="flex flex-wrap items-center gap-2">
        {ticket.status === 'open' && (
          <button onClick={() => patchTicket({ status: 'resolved' })} className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500">Resolve</button>
        )}
        {ticket.status === 'resolved' && (
          <>
            <button onClick={() => patchTicket({ status: 'closed' })} className="rounded-md bg-zinc-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-500">Close</button>
            <button onClick={() => patchTicket({ status: 'open' })} className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500">Reopen</button>
          </>
        )}
        {ticket.status === 'closed' && (
          <button onClick={() => patchTicket({ status: 'open' })} className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500">Reopen</button>
        )}
        <div className="flex items-center gap-2 ml-auto">
          <label htmlFor="assignee" className="text-xs text-zinc-500">Assignee</label>
          {/* "" unassigns: the registry treats empty assignee as unset. */}
          <select
            id="assignee"
            value={ticket.assignee || ''}
            onChange={e => patchTicket({ assignee: e.target.value })}
            className="rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
          >
            <option value="">Unassigned</option>
            {ticket.assignee && !users.includes(ticket.assignee) && (
              <option value={ticket.assignee}>{ticket.assignee}</option>
            )}
            {users.map(u => <option key={u} value={u}>{u}</option>)}
          </select>
          {/* scope v1 (doc 261): a ticket is always in one group — it moves to
              a group the caller edits; there is no commons to clear to. */}
          <label htmlFor="scope" className="text-xs text-zinc-500 ml-1">Scope</label>
          <select
            id="scope"
            value={ticket.group_scope || ''}
            onChange={e => patchTicket({ group_scope: e.target.value })}
            title="Who can see this ticket"
            className="rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
          >
            {ticket.group_scope && !scopeGroups.includes(ticket.group_scope) && (
              <option value={ticket.group_scope}>{ticket.group_scope}</option>
            )}
            {scopeGroups.map(g => <option key={g} value={g}>group: {g}</option>)}
          </select>
        </div>
      </div>
      {actionError && <p className="text-xs text-red-400">{actionError}</p>}

      {/* Comments */}
      <div className="space-y-4">
        <h3 className="text-sm font-semibold text-zinc-100">{ticket.comments.length} comment{ticket.comments.length !== 1 ? 's' : ''}</h3>
        {ticket.comments.filter(c => !c.parent_comment_id).map(c => {
          const replies = ticket.comments.filter(r => r.parent_comment_id === c.id);
          const renderOne = (cc: Comment, isReply: boolean) => (
            <div
              key={cc.id}
              id={`comment-${cc.id}`}
              data-block
              className={`rounded-lg border bg-zinc-900/50 p-4 ${isReply ? 'ml-6 border-l-2 border-l-zinc-700' : ''} ${cc.id === firstNewId ? 'border-amber-500/50 ring-1 ring-amber-500/30' : 'border-zinc-800'}`}
            >
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <span className="text-sm font-medium text-zinc-200">{cc.author}{cc.home_thread_id != null ? '@' + cc.home_thread_id : ''}</span>
                {cc.session_fp && (
                  <span className="text-[10px] font-mono text-zinc-600 border border-zinc-800 rounded px-1" title="session fingerprint — which surface of this identity wrote it (#264)">
                    ·{cc.session_fp}
                  </span>
                )}
                <span className="text-xs text-zinc-600">{fmtLocal(cc.created_at)}</span>
                {cc.id === firstNewId && <span className="text-[10px] uppercase tracking-wide text-amber-400">new since last visit</span>}
                <button
                  onClick={() => { setReplyTo(cc); document.getElementById('comment-box')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); }}
                  className="text-[10px] text-zinc-600 hover:text-zinc-300 uppercase tracking-wide ml-auto"
                >
                  reply
                </button>
              </div>
              <Markdown muted>{cc.body}</Markdown>
            </div>
          );
          return (
            <div key={c.id} className="space-y-2">
              {renderOne(c, false)}
              {replies.map(r => renderOne(r, true))}
            </div>
          );
        })}
      </div>

      {/* Add comment */}
      <form onSubmit={handleComment} className="space-y-3">
        {replyTo && (
          <div className="flex items-center gap-2 text-xs text-zinc-400 border border-zinc-800 rounded-md px-2 py-1 bg-zinc-900/60">
            <span>replying to <span className="text-zinc-200">{replyTo.author}</span>: “{replyTo.body.slice(0, 80)}{replyTo.body.length > 80 ? '…' : ''}”</span>
            <button type="button" onClick={() => setReplyTo(null)} className="ml-auto text-zinc-600 hover:text-zinc-300">×</button>
          </div>
        )}
        <textarea
          id="comment-box"
          value={commentBody}
          onChange={e => setCommentBody(e.target.value)}
          placeholder="Add a comment..."
          rows={3}
          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none resize-none"
        />
        {commentError && <p className="text-xs text-red-400">{commentError}</p>}
        <AttachButton
          linkTo={{ target_type: 'ticket', target_id: String(ticketId) }}
          onInsert={md => setCommentBody(b => (b ? `${b}\n\n${md}` : md))}
        />
        <button type="submit" disabled={commenting || !commentBody.trim()}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">
          {commenting ? 'Posting...' : 'Comment'}
        </button>
      </form>
    </div>
  );
}
