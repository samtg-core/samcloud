'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { useEvents } from '@/hooks/useEvents';
import { Markdown } from '@/components/Markdown';
import { AttachButton } from '@/components/AttachButton';
import type { DocDetail, DocRevisionMeta } from '@/lib/registry';

const PROXY = '/api/registry';

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

function linkHref(type: string, id: string): string | null {
  if (type === 'ticket') return `/tickets/${id}`;
  if (type === 'doc') return `/docs/${id}`;
  return null;
}

export default function DocDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const [docId, setDocId] = useState<string>('');
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [revisions, setRevisions] = useState<DocRevisionMeta[]>([]);
  const [viewRev, setViewRev] = useState<number | null>(null);  // null = head
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');

  // Edit is a toggle on the document itself (double-click / Edit), not a
  // permanent second field. The editor pre-fills from the current body and
  // stays in sync until the user types (dirty), so a background reload won't
  // clobber an in-progress edit.
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState('');
  const [editNote, setEditNote] = useState('');
  const [saving, setSaving] = useState(false);
  const dirty = useRef(false);

  const [commentBody, setCommentBody] = useState('');
  const [commenting, setCommenting] = useState(false);

  const { subscribe, connected } = useEvents();

  const loadDoc = useCallback(async (rev: number | null) => {
    if (!docId) return;
    const q = rev != null ? `?rev=${rev}` : '';
    try {
      const res = await fetch(`${PROXY}?path=${encodeURIComponent(`/api/v1/docs/${docId}${q}`)}`);
      if (res.ok) {
        const d: DocDetail = await res.json();
        setDoc(d);
        if (rev == null && !dirty.current) setEditBody(d.body);  // keep editor primed
      } else {
        const e = await res.json().catch(() => ({}));
        setError(e.detail || 'Could not load doc');
      }
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, [docId]);

  const loadHistory = useCallback(async () => {
    if (!docId) return;
    try {
      const res = await fetch(`${PROXY}?path=${encodeURIComponent(`/api/v1/docs/${docId}/revisions`)}`);
      if (res.ok) {
        const d = await res.json();
        setRevisions(Array.isArray(d?.revisions) ? d.revisions : []);
      }
    } catch { /* non-fatal */ }
  }, [docId]);

  useEffect(() => { params.then(({ id }) => setDocId(id)); }, [params]);
  useEffect(() => { if (docId) { loadDoc(viewRev); loadHistory(); } }, [docId, viewRev, loadDoc, loadHistory]);

  // Live updates — a revision/comment/update by anyone reloads head + history
  // (but not while you're mid-edit, so it can't yank your text).
  useEffect(() => {
    const unsub = subscribe('doc.*', (e) => {
      if (e.data?.id?.toString() === docId && !dirty.current) { loadDoc(null); loadHistory(); setViewRev(null); }
    });
    return unsub;
  }, [subscribe, docId, loadDoc, loadHistory]);

  async function mutate(method: string, path: string, body?: unknown): Promise<boolean> {
    setActionError('');
    try {
      const res = await fetch(`${PROXY}?path=${encodeURIComponent(path)}`, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setActionError(d.detail || `Failed (${res.status})`);
        return false;
      }
      return true;
    } catch {
      setActionError('Could not connect to registry');
      return false;
    }
  }

  async function handleSaveEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editBody.trim()) return;
    setSaving(true);
    const ok = await mutate('POST', `/api/v1/docs/${docId}/revisions`, { body: editBody, note: editNote || null });
    if (ok) { dirty.current = false; setEditNote(''); setEditing(false); setViewRev(null); await loadDoc(null); await loadHistory(); }
    setSaving(false);
  }

  function cancelEdit() {
    dirty.current = false;
    if (doc) setEditBody(doc.body);
    setEditNote('');
    setEditing(false);
    setActionError('');
  }

  async function handleComment(e: React.FormEvent) {
    e.preventDefault();
    if (!commentBody.trim()) return;
    setCommenting(true);
    const ok = await mutate('POST', `/api/v1/docs/${docId}/comments`, { body: commentBody });
    if (ok) { setCommentBody(''); await loadDoc(viewRev); }
    setCommenting(false);
  }

  async function handleStatus(status: string) {
    if (await mutate('PATCH', `/api/v1/docs/${docId}`, { status })) await loadDoc(viewRev);
  }

  async function handleAddLink(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const target_type = fd.get('target_type') as string;
    const target_id = (fd.get('target_id') as string)?.trim();
    if (!target_id) return;
    if (await mutate('POST', `/api/v1/docs/${docId}/links`, { target_type, target_id })) {
      e.currentTarget.reset();
      await loadDoc(viewRev);
    }
  }

  async function handleRemoveLink(linkId: number) {
    if (await mutate('DELETE', `/api/v1/docs/${docId}/links/${linkId}`)) await loadDoc(viewRev);
  }

  if (loading) return <div className="max-w-5xl mx-auto"><p className="text-zinc-500">Loading…</p></div>;
  if (error) return <div className="max-w-5xl mx-auto"><div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6"><p className="text-red-400">{error}</p></div></div>;
  if (!doc) return null;

  const isHead = doc.rev === doc.current_rev;

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center gap-3 text-sm">
        <a href="/docs" className="text-zinc-500 hover:text-zinc-300">Docs</a>
        <span className="text-zinc-700">/</span>
        {doc.parent && (
          <>
            <a href={`/docs/${doc.parent.id}`} className="text-zinc-500 hover:text-zinc-300 truncate max-w-48" title={doc.parent.title}>{doc.parent.title}</a>
            <span className="text-zinc-700">/</span>
          </>
        )}
        <span className="text-zinc-400 font-mono">{doc.slug}</span>
        {connected && <span className="inline-flex items-center gap-1 text-xs text-emerald-500 ml-auto"><span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />live</span>}
      </div>

      {/* Header */}
      <div>
        <h2 className="text-xl font-bold text-zinc-100">{doc.title}</h2>
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${KIND_STYLES[doc.kind] || KIND_STYLES.doc}`}>{doc.kind}</span>
          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${STATUS_STYLES[doc.status] || STATUS_STYLES.draft}`}>{doc.status}</span>
          <span className="text-xs text-zinc-500">owner {doc.source}</span>
          <span className="text-xs text-zinc-500">{doc.group_scope ? `group:${doc.group_scope}` : 'commons'}</span>
          <span className="text-xs text-zinc-500">{doc.revision_count} revision{doc.revision_count !== 1 ? 's' : ''}</span>
        </div>
        {(doc.children?.length ?? 0) > 0 && (
          <div className="flex flex-wrap items-center gap-2 mt-3">
            <span className="text-xs uppercase tracking-wide text-zinc-600">sub-docs</span>
            {doc.children!.map((c) => (
              <a key={c.id} href={`/docs/${c.id}`}
                 className="inline-flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800/50 transition-colors">
                <span className={`inline-block w-1.5 h-1.5 rounded-full ${c.status === 'active' ? 'bg-emerald-500' : 'bg-zinc-600'}`} />
                {c.title}
              </a>
            ))}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_220px] gap-6">
        {/* Document + linked + comments */}
        <div className="space-y-5 min-w-0">
          {!isHead && (
            <div className="rounded-md border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-xs text-amber-400">
              Viewing historical revision v{doc.rev} (head is v{doc.current_rev}).{' '}
              <button onClick={() => setViewRev(null)} className="underline hover:text-amber-300">Back to current</button>
            </div>
          )}

          {/* The document — rendered markdown; double-click (or Edit) to revise */}
          {editing ? (
            <form onSubmit={handleSaveEdit} className="space-y-3">
              <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide">
                Editing — Save creates revision v{doc.current_rev + 1}
              </h3>
              <textarea
                value={editBody}
                autoFocus
                onChange={e => { dirty.current = true; setEditBody(e.target.value); }}
                rows={Math.min(40, Math.max(14, editBody.split('\n').length + 2))}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none resize-y font-mono leading-relaxed"
              />
              <input
                value={editNote}
                onChange={e => setEditNote(e.target.value)}
                placeholder="Edit summary (why) — optional"
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
              />
              <div className="flex items-center gap-3">
                <AttachButton
                  linkTo={{ target_type: 'doc', target_id: String(docId) }}
                  onInsert={md => { dirty.current = true; setEditBody(b => (b ? `${b}\n\n${md}` : md)); }}
                />
                <button type="submit" disabled={saving || !editBody.trim()}
                  className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">
                  {saving ? 'Saving…' : 'Save changes'}
                </button>
                <button type="button" onClick={cancelEdit} className="text-xs text-zinc-500 hover:text-zinc-300">Cancel</button>
              </div>
            </form>
          ) : (
            <div className="space-y-1">
              {isHead && (
                <div className="flex items-center justify-end gap-2">
                  <span className="text-xs text-zinc-600 hidden sm:inline">double-click the doc to edit</span>
                  <button onClick={() => setEditing(true)}
                    className="rounded-md border border-zinc-800 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800">Edit</button>
                </div>
              )}
              <div
                onDoubleClick={() => { if (isHead) setEditing(true); }}
                className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-5 py-4"
                title={isHead ? 'double-click to edit' : undefined}
              >
                {doc.body.trim()
                  ? <Markdown>{doc.body}</Markdown>
                  : <p className="text-sm text-zinc-600 italic">empty document — double-click to write</p>}
              </div>
            </div>
          )}

          {actionError && <p className="text-xs text-red-400">{actionError}</p>}

          {/* Linked items */}
          <div className="space-y-2">
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide">Linked</h3>
            <div className="flex flex-wrap gap-2">
              {doc.links.length === 0 && <span className="text-xs text-zinc-600">nothing linked yet</span>}
              {doc.links.map((l) => {
                const href = linkHref(l.target_type, l.target_id);
                const label = `${l.target_type}:${l.target_type === 'ticket' ? '#' : ''}${l.target_id}`;
                return (
                  <span key={l.id} className="inline-flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-xs text-zinc-300">
                    {href ? <a href={href} className="hover:text-white">{label}</a> : label}
                    {l.id != null && (
                      <button onClick={() => handleRemoveLink(l.id!)} className="text-zinc-600 hover:text-red-400" title="remove link">✕</button>
                    )}
                  </span>
                );
              })}
            </div>
            <form onSubmit={handleAddLink} className="flex gap-2 items-center pt-1">
              <select name="target_type" defaultValue="ticket"
                className="rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-xs text-zinc-100 focus:border-zinc-600 focus:outline-none">
                <option value="ticket">ticket</option>
                <option value="service">service</option>
                <option value="device">device</option>
                <option value="doc">doc</option>
              </select>
              <input name="target_id" placeholder="id / slug"
                className="flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-xs text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none" />
              <button type="submit" className="rounded-md bg-zinc-800 px-3 py-1 text-xs text-zinc-100 hover:bg-zinc-700">Link</button>
            </form>
          </div>

          {/* Comments — the discussion layer, distinct from the body */}
          <div className="space-y-3 pt-2 border-t border-zinc-800">
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide pt-3">
              {doc.comments.length} comment{doc.comments.length !== 1 ? 's' : ''}
            </h3>
            {doc.comments.map(c => (
              <div key={c.id} data-block className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-zinc-200">{c.author}{c.home_thread_id != null ? '@' + c.home_thread_id : ''}{c.session_fp ? ' [' + c.session_fp + ']' : ''}</span>
                  <span className="text-xs text-zinc-600">{c.created_at}</span>
                </div>
                <Markdown>{c.body}</Markdown>
              </div>
            ))}
            <form onSubmit={handleComment} className="space-y-2">
              <textarea
                value={commentBody}
                onChange={e => setCommentBody(e.target.value)}
                placeholder="Add a comment (discussion, not an edit)…"
                rows={2}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none resize-none"
              />
              <AttachButton
                linkTo={{ target_type: 'doc', target_id: String(docId) }}
                onInsert={md => setCommentBody(b => (b ? `${b}\n\n${md}` : md))}
              />
              <button type="submit" disabled={commenting || !commentBody.trim()}
                className="rounded-md bg-zinc-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-600 disabled:opacity-50">
                {commenting ? 'Posting…' : 'Comment'}
              </button>
            </form>
          </div>
        </div>

        {/* Sidebar: history + lifecycle + notifications */}
        <div className="space-y-5">
          <div className="space-y-2">
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide">History</h3>
            <div className="space-y-1">
              {revisions.map(r => (
                <button
                  key={r.rev}
                  onClick={() => setViewRev(r.rev === doc.current_rev ? null : r.rev)}
                  className={`block w-full text-left rounded-md border px-2 py-1.5 text-xs transition-colors ${
                    r.rev === doc.rev ? 'border-zinc-600 bg-zinc-800 text-zinc-100' : 'border-zinc-800 text-zinc-400 hover:bg-zinc-800/50'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono">v{r.rev}{r.rev === doc.current_rev ? ' (head)' : ''}</span>
                    <span className="text-zinc-600">{r.author}</span>
                  </div>
                  {r.note && <div className="text-zinc-500 truncate mt-0.5">{r.note}</div>}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide">Lifecycle</h3>
            <div className="flex flex-wrap gap-2">
              {doc.status !== 'active' && (
                <button onClick={() => handleStatus('active')} className="rounded-md bg-emerald-600/90 px-2.5 py-1 text-xs text-white hover:bg-emerald-500">Activate</button>
              )}
              {doc.status !== 'archived' && (
                <button onClick={() => handleStatus('archived')} className="rounded-md bg-zinc-700 px-2.5 py-1 text-xs text-white hover:bg-zinc-600">Archive</button>
              )}
            </div>
            {doc.supersedes != null && (
              <p className="text-xs text-zinc-500">supersedes <a href={`/docs/${doc.supersedes}`} className="text-zinc-300 hover:text-white">doc {doc.supersedes}</a></p>
            )}
          </div>

          <div className="space-y-2">
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide">Notifications</h3>
            <div className="flex gap-2">
              <button onClick={() => mutate('POST', `/api/v1/docs/${docId}/subscribe`)}
                className="rounded-md border border-zinc-800 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800">Subscribe</button>
              <button onClick={() => mutate('DELETE', `/api/v1/docs/${docId}/subscribe`)}
                className="rounded-md border border-zinc-800 px-2.5 py-1 text-xs text-zinc-500 hover:bg-zinc-800">Unsubscribe</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
