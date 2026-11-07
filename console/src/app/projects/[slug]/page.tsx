'use client';

import { use, useCallback, useEffect, useState } from 'react';
import type { ProjectDetail, ProjectTask } from '@/lib/registry';
import { ProgressBar } from '../ProgressBar';

const TASK_STATUSES = ['todo', 'in_progress', 'blocked', 'done', 'cancelled'];

const STATUS_ICON: Record<string, string> = {
  todo: '○',
  in_progress: '◐',
  blocked: '◻',
  done: '●',
  cancelled: '×',
};

const STATUS_COLOR: Record<string, string> = {
  todo: 'text-zinc-500',
  in_progress: 'text-amber-400',
  blocked: 'text-orange-400',
  done: 'text-emerald-400',
  cancelled: 'text-zinc-600',
};

async function registryFetch(path: string, method = 'GET', body?: unknown) {
  const res = await fetch(`/api/registry?path=${encodeURIComponent(path)}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: 'no-store',
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(typeof d?.detail === 'string' ? d.detail : `Request failed (${res.status})`);
  }
  return res.json();
}

export default function ProjectPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = use(params);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [sortBy, setSortBy] = useState('position');
  const [collapse, setCollapse] = useState<{ mode: boolean; n: number }>({ mode: false, n: 0 });
  const [statusF, setStatusF] = useState('');
  const [assigneeF, setAssigneeF] = useState('');
  const [hideDone, setHideDone] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await registryFetch(`/api/v1/projects/${slug}`);
      setProject(data);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load project');
    } finally {
      setLoading(false);
    }
  }, [slug]);

  useEffect(() => {
    load();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, [load]);

  if (loading) return <div className="max-w-4xl mx-auto p-8 text-center text-zinc-600 text-sm">Loading…</div>;
  if (error || !project) {
    return (
      <div className="max-w-4xl mx-auto p-8 text-center text-sm text-red-400">
        {error || 'Project not found'} — <a href="/projects" className="text-zinc-400 hover:text-white underline">back to projects</a>
      </div>
    );
  }

  const r = project.rollup;

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div>
        <a href="/projects" className="text-xs text-zinc-500 hover:text-zinc-300">← Projects</a>
        <div className="mt-2 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-2xl font-bold text-zinc-100">{project.name}</h2>
            {project.description && <p className="text-zinc-500 mt-1 text-sm">{project.description}</p>}
            <div className="flex items-center gap-3 text-xs text-zinc-500 mt-2 flex-wrap">
              <span className="font-mono text-zinc-600">{project.slug}</span>
              <span>{project.status}</span>
              <span>{project.group_scope ? `group:${project.group_scope}` : 'unscoped'}</span>
              <span>by {project.source}</span>
            </div>
          </div>
          <div className="w-48 shrink-0 space-y-1">
            {r.task_count > 0 && <ProgressBar pct={r.progress_pct} basis={r.progress_basis} />}
            <div className="text-xs text-zinc-500 text-right">
              {r.task_count} task{r.task_count === 1 ? '' : 's'}
              {r.estimate_total_min ? ` · ~${Math.round(r.estimate_total_min / 60 * 10) / 10}h est` : ''}
            </div>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <AddTaskForm slug={project.slug} parentId={null} onDone={load} standalone />
        <span className="flex-1" />
        <select value={statusF} onChange={(e) => setStatusF(e.target.value)}
                className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-100 focus:border-zinc-600 focus:outline-none">
          <option value="">any status</option>
          {TASK_STATUSES.map(st => <option key={st} value={st}>{st.replace('_', ' ')}</option>)}
        </select>
        <select value={assigneeF} onChange={(e) => setAssigneeF(e.target.value)}
                className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-100 focus:border-zinc-600 focus:outline-none">
          <option value="">any assignee</option>
          {collectAssignees(project.tasks).map(a => <option key={a} value={a}>{a}</option>)}
        </select>
        <button onClick={() => setHideDone(h => !h)}
                className={`text-xs rounded-md px-2 py-1 border ${hideDone ? 'border-zinc-500 text-zinc-100 bg-zinc-800' : 'border-zinc-800 text-zinc-500 hover:text-white'}`}>
          hide done
        </button>
        <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}
                className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-100 focus:border-zinc-600 focus:outline-none">
          <option value="position">board order</option>
          <option value="created:desc">newest created</option>
          <option value="created:asc">oldest created</option>
          <option value="updated:desc">recently updated</option>
          <option value="estimate:asc">smallest estimate</option>
        </select>
        <button onClick={() => setCollapse(c => ({ mode: true, n: c.n + 1 }))}
                className="text-xs text-zinc-500 hover:text-white border border-zinc-800 rounded-md px-2 py-1">collapse all</button>
        <button onClick={() => setCollapse(c => ({ mode: false, n: c.n + 1 }))}
                className="text-xs text-zinc-500 hover:text-white border border-zinc-800 rounded-md px-2 py-1">expand all</button>
      </div>

      <div className="space-y-1">
        {project.tasks.length > 0 ? (
          sortTasks(filterTasks(project.tasks, statusF, assigneeF, hideDone), sortBy).map(t => (
            <TaskNode key={t.id} task={t} slug={project.slug} depth={0} onChanged={load} sortBy={sortBy} collapse={collapse} />
          ))
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">
            No tasks yet — add the first one above
          </div>
        )}
      </div>
    </div>
  );
}

function collectAssignees(tasks: ProjectTask[]): string[] {
  const out = new Set<string>();
  const walk = (ts: ProjectTask[]) => ts.forEach(t => {
    if (t.assignee) out.add(t.assignee);
    if (t.subtasks) walk(t.subtasks);
  });
  walk(tasks);
  return [...out].sort();
}

// A node survives the filter if it matches OR any descendant does — so
// filtering never orphans a matching subtask from its context.
function filterTasks(tasks: ProjectTask[], status: string, assignee: string, hideDone: boolean): ProjectTask[] {
  const matches = (t: ProjectTask): boolean => {
    if (hideDone && (t.status === 'done' || t.status === 'cancelled')) return false;
    if (status && t.status !== status) return false;
    if (assignee && t.assignee !== assignee) return false;
    return true;
  };
  const walk = (ts: ProjectTask[]): ProjectTask[] =>
    ts.map(t => {
      const kids = walk(t.subtasks || []);
      if (!matches(t) && kids.length === 0) return null;
      return { ...t, subtasks: kids };
    }).filter(Boolean) as ProjectTask[];
  return walk(tasks);
}

function sortTasks(tasks: ProjectTask[], sortBy: string): ProjectTask[] {
  if (sortBy === 'position') return tasks;
  const [field, dir] = sortBy.split(':');
  const key = (t: ProjectTask) =>
    field === 'created' ? t.created_at
    : field === 'updated' ? t.updated_at
    : String(t.estimate_min ?? 999999).padStart(7, '0');
  return [...tasks].sort((a, b) => {
    const cmp = String(key(a) ?? '').localeCompare(String(key(b) ?? ''));
    return dir === 'asc' ? cmp : -cmp;
  });
}

function TaskNode({ task, slug, depth, onChanged, sortBy, collapse }: {
  task: ProjectTask; slug: string; depth: number; onChanged: () => void;
  sortBy: string; collapse: { mode: boolean; n: number };
}) {
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [folded, setFolded] = useState(false);
  useEffect(() => {
    if (collapse.n > 0) setFolded(collapse.mode);
  }, [collapse]);

  async function setStatus(status: string) {
    setBusy(true);
    setMenuOpen(false);
    try {
      await registryFetch(`/api/v1/tasks/${task.id}`, 'PATCH', { status });
      onChanged();
    } catch { /* transient */ } finally {
      setBusy(false);
    }
  }

  // Click the icon: advance todo → in_progress → done → todo.
  function advance() {
    const next = task.status === 'todo' ? 'in_progress' : task.status === 'in_progress' ? 'done' : 'todo';
    setStatus(next);
  }

  const dim = task.status === 'done' || task.status === 'cancelled';

  return (
    <div style={{ marginLeft: depth * 24 }}>
      <div className={`group flex items-center gap-2 rounded-lg border border-zinc-800/60 bg-zinc-900/40 px-3 py-2 hover:bg-zinc-800/30 transition-colors ${busy ? 'opacity-50' : ''}`}>
        <div className="relative shrink-0">
          <span className="inline-flex items-center">
            <button onClick={advance} onContextMenu={(e) => { e.preventDefault(); setMenuOpen(v => !v); }}
                    title={`${task.status} — click to advance`}
                    className={`text-base leading-none w-5 ${STATUS_COLOR[task.status] || 'text-zinc-500'} hover:scale-110 transition-transform`}>
              {STATUS_ICON[task.status] || '○'}
            </button>
            <button onClick={() => setMenuOpen(v => !v)} title="set status"
                    className="text-[9px] text-zinc-600 hover:text-zinc-300 px-0.5">▾</button>
          </span>
          {menuOpen && (
            <div className="absolute z-10 top-6 left-0 rounded-lg border border-zinc-700 bg-zinc-900 shadow-xl py-1 text-xs">
              {TASK_STATUSES.map(s => (
                <button key={s} onClick={() => setStatus(s)}
                        className={`block w-full text-left px-3 py-1 hover:bg-zinc-800 ${s === task.status ? 'text-zinc-100' : 'text-zinc-400'}`}>
                  <span className={STATUS_COLOR[s]}>{STATUS_ICON[s]}</span> {s.replace('_', ' ')}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="min-w-0 flex-1">
          <a href={`/tasks/${task.id}`}
             className={`text-sm hover:underline ${dim ? 'text-zinc-500 line-through decoration-zinc-700' : 'text-zinc-200 hover:text-white'}`}>
            {task.title}
          </a>
          <div className="flex items-center gap-2.5 text-[11px] text-zinc-600 flex-wrap">
            {task.assignee && <span className="text-zinc-500">@{task.assignee}</span>}
            {task.estimate_min ? <span>{task.estimate_min}m</span> : null}
            {task.due && <span>due {task.due}</span>}
            {task.priority && <span className={task.priority === 'high' ? 'text-red-400' : ''}>{task.priority}</span>}
            {task.delegated_ticket_id && (
              <a href={`/tickets/${task.delegated_ticket_id}`} className="text-blue-400/80 hover:text-blue-300">
                ↳ ticket #{task.delegated_ticket_id}
              </a>
            )}
            {task.source_ref && <span className="font-mono">{task.source_ref}</span>}
          </div>
        </div>

        {(task.subtasks || []).length > 0 && (
          <button onClick={() => setFolded(f => !f)}
                  title={folded ? `show ${task.subtasks!.length} subtasks` : 'collapse subtasks'}
                  className="text-xs text-zinc-600 hover:text-zinc-300 shrink-0">
            {folded ? `▸ ${task.subtasks!.length}` : '▾'}
          </button>
        )}
        <button onClick={() => setAdding(v => !v)}
                className="opacity-0 group-hover:opacity-100 text-xs text-zinc-500 hover:text-zinc-200 shrink-0 transition-opacity">
          + sub
        </button>
      </div>

      {adding && (
        <div style={{ marginLeft: 24 }} className="mt-1">
          <AddTaskForm slug={slug} parentId={task.id} onDone={() => { setAdding(false); onChanged(); }} />
        </div>
      )}

      {!folded && (task.subtasks || []).length > 0 && (
        <div className="mt-1 space-y-1">
          {sortTasks(task.subtasks!, sortBy).map(st => (
            <TaskNode key={st.id} task={st} slug={slug} depth={depth + 1} onChanged={onChanged} sortBy={sortBy} collapse={collapse} />
          ))}
        </div>
      )}
    </div>
  );
}

function AddTaskForm({ slug, parentId, onDone, standalone }: {
  slug: string; parentId: number | null; onDone: () => void; standalone?: boolean;
}) {
  const [open, setOpen] = useState(!standalone);
  const [title, setTitle] = useState('');
  const [estimate, setEstimate] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    setError('');
    try {
      await registryFetch(`/api/v1/projects/${slug}/tasks`, 'POST', {
        title: title.trim(),
        parent_task_id: parentId,
        estimate_min: estimate ? parseInt(estimate, 10) : null,
      });
      setTitle(''); setEstimate('');
      if (standalone) setOpen(false);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed');
    } finally {
      setBusy(false);
    }
  }

  if (standalone && !open) {
    return (
      <button onClick={() => setOpen(true)}
              className="text-sm text-zinc-400 hover:text-white border border-zinc-800 hover:border-zinc-600 rounded-lg px-4 py-2 transition-colors">
        + Add task
      </button>
    );
  }

  return (
    <form onSubmit={submit} className="flex items-center gap-2">
      <input autoFocus value={title} onChange={e => setTitle(e.target.value)}
             placeholder={parentId ? 'Subtask title' : 'Task title'}
             className="flex-1 bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-600" />
      <input value={estimate} onChange={e => setEstimate(e.target.value.replace(/\D/g, ''))}
             placeholder="min" inputMode="numeric"
             className="w-16 bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-600" />
      <button type="submit" disabled={busy || !title.trim()}
              className="text-sm bg-zinc-100 text-zinc-900 rounded px-3 py-1.5 font-medium disabled:opacity-50">
        {busy ? '…' : 'Add'}
      </button>
      {standalone && (
        <button type="button" onClick={() => setOpen(false)} className="text-sm text-zinc-500 hover:text-zinc-300 px-1">
          Cancel
        </button>
      )}
      {error && <span className="text-xs text-red-400">{error}</span>}
    </form>
  );
}
