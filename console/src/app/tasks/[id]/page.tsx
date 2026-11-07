'use client';

// Task detail view (#203 feedback: tasks had no clickable landing page —
// metadata and descriptions existed in the API but nothing rendered them).
// Read-focused; editing stays on the project tree page.

import { use, useCallback, useEffect, useState } from 'react';
import { Markdown } from '@/components/Markdown';
import { fmtLocal } from '@/lib/time';
import { AttachButton } from '@/components/AttachButton';

interface Subtask { id: number; title: string; status: string; }
interface TaskComment { id: number; author: string; body: string; created_at: string; session_fp?: string | null; home_thread_id?: number | null; }
interface TaskDetail {
  id: number;
  title: string;
  detail: string | null;
  status: string;
  priority: string | null;
  estimate_min: number | null;
  actual_min: number | null;
  assignee: string | null;
  due: string | null;
  source: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  project_id: number;
  project_slug: string;
  parent_task_id: number | null;
  delegated_ticket_id: number | null;
  tags?: string[];
  subtasks: Subtask[];
  comments?: TaskComment[];
}

const STATUS_STYLES: Record<string, string> = {
  todo: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-300',
  in_progress: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
  blocked: 'bg-red-500/10 border-red-500/20 text-red-400',
  done: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
  cancelled: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-500',
};

const STATUSES = ['todo', 'in_progress', 'blocked', 'done', 'cancelled'];

/**
 * The status pill is a control, not a label (#382): click it, pick the new
 * status, it PATCHes the task in place — no round-trip via the project tree.
 */
function StatusPill({ taskId, status, onChanged }: { taskId: string; status: string; onChanged: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  async function pick(next: string) {
    if (next === status) { setOpen(false); return; }
    setSaving(true);
    try {
      await fetch(`/api/registry?path=${encodeURIComponent(`/api/v1/tasks/${taskId}`)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: next }),
      });
      await onChanged();
    } finally {
      setSaving(false);
      setOpen(false);
    }
  }

  return (
    <span className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={saving}
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 transition-colors hover:brightness-125 disabled:opacity-50 ${STATUS_STYLES[status] || STATUS_STYLES.todo}`}
      >
        {saving ? '…' : status.replace('_', ' ')}
        <span className="text-[9px] opacity-60">▾</span>
      </button>
      {open && (
        <>
          <span className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <span className="absolute left-0 top-full z-20 mt-1 flex min-w-32 flex-col rounded-lg border border-zinc-700 bg-zinc-900 py-1 shadow-xl">
            {STATUSES.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => pick(s)}
                className={`px-3 py-1.5 text-left text-xs transition-colors hover:bg-zinc-800 ${s === status ? 'text-zinc-100 font-semibold' : 'text-zinc-400'}`}
              >
                {s.replace('_', ' ')}
              </button>
            ))}
          </span>
        </>
      )}
    </span>
  );
}

export default function TaskPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [comment, setComment] = useState('');
  const [posting, setPosting] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/registry?path=${encodeURIComponent(`/api/v1/tasks/${id}`)}`, { cache: 'no-store' });
      if (res.ok) setTask(await res.json());
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="max-w-3xl mx-auto p-8 text-center text-zinc-600 text-sm">Loading task…</div>;
  if (!task) return <div className="max-w-3xl mx-auto p-8 text-center text-zinc-600 text-sm">Task not found (or not in your scope).</div>;

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-2 text-sm text-zinc-500">
        <a href="/inbox" className="hover:text-white">Inbox</a>
        <span>/</span>
        <a href={`/projects/${task.project_slug}`} className="hover:text-white">{task.project_slug}</a>
        <span>/</span>
        <span className="text-zinc-400">task {task.id}</span>
      </div>

      <div>
        <h2 className="text-xl font-bold text-zinc-100">{task.title}</h2>
        <div className="flex items-center gap-2 mt-2 flex-wrap text-xs">
          <StatusPill taskId={id} status={task.status} onChanged={load} />
          {task.priority && <span className="text-zinc-500">priority: {task.priority}</span>}
          {task.estimate_min != null && <span className="text-zinc-500">~{task.estimate_min}min</span>}
          {task.assignee && <span className="text-zinc-500">assignee: {task.assignee}</span>}
          {task.source && <span className="text-zinc-600">by {task.source}</span>}
          {task.due && <span className="text-orange-400">due {task.due}</span>}
          {task.tags?.map((t) => (
            <span key={t} className="inline-flex items-center rounded-full bg-blue-500/10 border border-blue-500/30 text-blue-300 px-2 py-0.5">
              {t}
            </span>
          ))}
        </div>
      </div>

      {task.detail ? (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-sm text-zinc-300">
          <Markdown>{task.detail}</Markdown>
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-sm text-zinc-600">No description.</div>
      )}

      {task.delegated_ticket_id && (
        <a href={`/tickets/${task.delegated_ticket_id}`} className="block rounded-lg border border-purple-500/30 bg-purple-500/5 p-3 text-sm text-zinc-300 hover:bg-zinc-800/30 transition-colors">
          Delegated → ticket #{task.delegated_ticket_id} (the waiting handle — conversation lives there)
        </a>
      )}

      {task.subtasks?.length > 0 && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">Subtasks ({task.subtasks.length})</h3>
          {task.subtasks.map((s) => (
            <a key={s.id} href={`/tasks/${s.id}`} className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 text-sm hover:bg-zinc-800/30 transition-colors">
              <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${STATUS_STYLES[s.status] || STATUS_STYLES.todo}`}>
                {s.status.replace('_', ' ')}
              </span>
              <span className="text-zinc-200">{s.title}</span>
            </a>
          ))}
        </section>
      )}

      {/* Comments — same conversational layer as tickets/docs (#203 riff) */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          Comments ({task.comments?.length ?? 0})
        </h3>
        {(task.comments ?? []).map((c) => (
          <div key={c.id} data-block className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3">
            <div className="text-xs text-zinc-500 mb-1">{c.author}{c.home_thread_id != null ? '@' + c.home_thread_id : ''}{c.session_fp ? ' [' + c.session_fp + ']' : ''} · {fmtLocal(c.created_at)}</div>
            <div className="text-sm text-zinc-300"><Markdown>{c.body}</Markdown></div>
          </div>
        ))}
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (!comment.trim() || posting) return;
            setPosting(true);
            try {
              await fetch(`/api/registry?path=${encodeURIComponent(`/api/v1/tasks/${id}/comments`)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ body: comment }),
              });
              setComment('');
              await load();
            } finally {
              setPosting(false);
            }
          }}
          className="flex gap-2"
        >
          <input
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Comment… (notifies assignee + project watchers)"
            className="flex-1 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
          />
          <button
            type="submit"
            disabled={posting || !comment.trim()}
            className="rounded-md border border-zinc-700 px-3 py-2 text-sm text-zinc-300 hover:text-white disabled:opacity-40 transition-colors"
          >
            {posting ? '…' : 'Post'}
          </button>
        </form>
        {/* Tasks are the third primitive; they attach like tickets and docs (#328). */}
        <AttachButton
          linkTo={{ target_type: 'task', target_id: String(id) }}
          onInsert={(md) => setComment((c) => (c ? `${c} ${md}` : md))}
        />
      </section>

      <div className="text-xs text-zinc-600">
        created {fmtLocal(task.created_at)} · updated {fmtLocal(task.updated_at)}
        {task.completed_at && <> · completed {task.completed_at}</>}
      </div>
    </div>
  );
}
