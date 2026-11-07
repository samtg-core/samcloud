'use client';

import { useEffect, useState } from 'react';
import { useScopeGroups } from '@/lib/useScopeGroups';

export function CreateDocForm() {
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Groups you're allowed to scope to (writes stay membership-enforced
  // server-side). The shared hook handles the admin-wildcard case that made
  // this dropdown render empty for admins (#328).
  const groups = useScopeGroups(open);
  // Parent picker (H3): nest the new doc under an existing one. Loaded only
  // when the form opens; titles are the human handle, ids ride the payload.
  const [parents, setParents] = useState<{ id: number; title: string }[]>([]);
  useEffect(() => {
    if (!open) return;
    fetch('/api/registry?path=' + encodeURIComponent('/api/v1/docs?status=active'), { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : { docs: [] }))
      .then((d) => setParents((d.docs || []).map((x: { id: number; title: string }) => ({ id: x.id, title: x.title }))))
      .catch(() => {});
  }, [open]);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);

    const form = e.currentTarget;
    const fd = new FormData(form);
    const scope = (fd.get('group_scope') as string)?.trim();
    const kind = (fd.get('kind') as string)?.trim() || 'doc';
    // `source`/`author` are bearer-derived by the registry (extra fields → 422).
    const payload: Record<string, unknown> = {
      title: fd.get('title') as string,
      kind,
      body: fd.get('body') as string,
    };
    if (scope) payload.group_scope = scope;
    const parent = (fd.get('parent_doc_id') as string)?.trim();
    if (parent) payload.parent_doc_id = Number(parent);

    try {
      const res = await fetch('/api/registry?path=/api/v1/docs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Failed: ${res.status}`);
      window.location.href = `/docs/${data.doc_id}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create doc');
      setSubmitting(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-2.5 text-sm text-zinc-400 hover:text-white hover:border-zinc-700 transition-colors w-full text-left"
      >
        + New doc...
      </button>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-zinc-200">New Doc</h4>
        <button type="button" onClick={() => setOpen(false)} className="text-xs text-zinc-500 hover:text-zinc-300">
          Cancel
        </button>
      </div>

      <div className="space-y-3">
        <input
          name="title"
          required
          placeholder="Title"
          className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
        />
        <div className="flex gap-3">
          {/* kind is a free label (suggestions, not a fixed enum) */}
          <input
            name="kind"
            defaultValue="doc"
            list="doc-kinds"
            placeholder="kind"
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
          />
          <datalist id="doc-kinds">
            <option value="doc" /><option value="rfc" /><option value="runbook" /><option value="note" />
          </datalist>
          {/* group scope: a group you edit; unnamed = your first group (scope v1) */}
          <select
            name="group_scope"
            defaultValue=""
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
          >
            <option value="">my group (default)</option>
            {groups.map(g => <option key={g} value={g}>group: {g}</option>)}
          </select>
        </div>
        <select
          name="parent_doc_id"
          defaultValue=""
          className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
        >
          <option value="">top-level (no parent)</option>
          {parents.map((par) => <option key={par.id} value={par.id}>nest under: {par.title}</option>)}
        </select>
        <textarea
          name="body"
          required
          rows={8}
          placeholder="Markdown body…"
          className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none resize-y font-mono"
        />
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}

      <button type="submit" disabled={submitting}
        className="rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-100 hover:bg-zinc-700 transition-colors disabled:opacity-50">
        {submitting ? 'Creating...' : 'Create Doc'}
      </button>
    </form>
  );
}
