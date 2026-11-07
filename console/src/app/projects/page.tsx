'use client';

import { Suspense, useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import type { Project } from '@/lib/registry';
import { ProgressBar } from './ProgressBar';
import { useScopeFilter } from '@/lib/scopeFilter';

const STATUS_FILTERS = ['planning', 'active', 'paused', 'done', 'archived'];

const STATUS_STYLES: Record<string, string> = {
  planning: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
  active: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
  paused: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  done: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
  archived: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-500',
};

export default function ProjectsPage() {
  return (
    <Suspense fallback={<div className="max-w-4xl mx-auto p-8 text-center text-zinc-600 text-sm">Loading projects…</div>}>
      <ProjectsInner />
    </Suspense>
  );
}

function ProjectsInner() {
  const searchParams = useSearchParams();
  const filterStatus = searchParams.get('status') || undefined;

  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const scope = useScopeFilter();  // H2 (#380)

  const load = useCallback(async () => {
    const p = new URLSearchParams();
    if (filterStatus) p.set('status', filterStatus);
    if (scope) p.set('group_scope', scope);
    const qs = p.toString() ? `?${p.toString()}` : '';
    try {
      const res = await fetch(`/api/registry?path=${encodeURIComponent(`/api/v1/projects${qs}`)}`, { cache: 'no-store' });
      const data = res.ok ? await res.json() : { projects: [] };
      setProjects(Array.isArray(data?.projects) ? data.projects : []);
    } catch {
      setProjects([]);
    } finally {
      setLoading(false);
    }
  }, [filterStatus, scope]);

  useEffect(() => {
    setLoading(true);
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  function filterHref(status?: string) {
    return status ? `/projects?status=${status}` : '/projects';
  }

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Projects</h2>
          <p className="text-zinc-500 mt-1">The task plane — doc is a record, ticket is a request, task is an actionable item</p>
        </div>
        <div className="flex items-center gap-4 shrink-0 text-sm">
          <a href="/tickets" className="text-zinc-400 hover:text-white transition-colors">Tickets</a>
          <a href="/docs" className="text-zinc-400 hover:text-white transition-colors">Docs</a>
        </div>
      </div>

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

      <CreateProjectForm onCreated={load} />

      <div className="space-y-3">
        {loading ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">Loading…</div>
        ) : projects.length > 0 ? (
          projects.map((p) => <ProjectCard key={p.id} project={p} />)
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-8 text-center text-zinc-600 text-sm">
            {filterStatus ? `No ${filterStatus} projects` : 'No projects yet'}
          </div>
        )}
      </div>
    </div>
  );
}

function ProjectCard({ project }: { project: Project }) {
  const r = project.rollup;
  const open = r ? (r.by_status['todo'] || 0) + (r.by_status['in_progress'] || 0) + (r.by_status['blocked'] || 0) : 0;
  return (
    <a
      href={`/projects/${project.slug}`}
      className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/30 transition-colors"
    >
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0 space-y-2 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-zinc-200">{project.name}</span>
            <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${STATUS_STYLES[project.status] || STATUS_STYLES.active}`}>
              {project.status}
            </span>
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-500 flex-wrap">
            <span className="font-mono text-zinc-600">{project.slug}</span>
            {r && <span>{r.task_count} task{r.task_count === 1 ? '' : 's'}{open > 0 ? ` · ${open} open` : ''}</span>}
            {r?.estimate_total_min ? <span>~{Math.round(r.estimate_total_min / 60 * 10) / 10}h</span> : null}
            <span>{project.group_scope ? `group:${project.group_scope}` : 'unscoped'}</span>
            <span>by {project.source}</span>
          </div>
        </div>
        <div className="w-36 shrink-0">
          {r && r.task_count > 0 && <ProgressBar pct={r.progress_pct} basis={r.progress_basis} />}
        </div>
      </div>
    </a>
  );
}

function CreateProjectForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError('');
    try {
      const res = await fetch(`/api/registry?path=${encodeURIComponent('/api/v1/projects')}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), description: description.trim() || null }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(typeof d?.detail === 'string' ? d.detail : `Create failed (${res.status})`);
      }
      setName(''); setDescription(''); setOpen(false);
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Create failed');
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
              className="text-sm text-zinc-400 hover:text-white border border-zinc-800 hover:border-zinc-600 rounded-lg px-4 py-2 transition-colors">
        + New project
      </button>
    );
  }
  return (
    <form onSubmit={submit} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
      <input autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="Project name"
             className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-600" />
      <input value={description} onChange={e => setDescription(e.target.value)} placeholder="Description (optional)"
             className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-600" />
      {error && <p className="text-xs text-red-400">{error}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={busy || !name.trim()}
                className="text-sm bg-zinc-100 text-zinc-900 rounded px-4 py-1.5 font-medium disabled:opacity-50">
          {busy ? 'Creating…' : 'Create'}
        </button>
        <button type="button" onClick={() => setOpen(false)} className="text-sm text-zinc-500 hover:text-zinc-300 px-2">
          Cancel
        </button>
      </div>
    </form>
  );
}
