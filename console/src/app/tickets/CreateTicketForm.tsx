'use client';

import { useState, useEffect } from 'react';
import { useScopeGroups } from '@/lib/useScopeGroups';

export function CreateTicketForm() {
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [users, setUsers] = useState<string[]>([]);
  // scope v1 (doc 261): scope a ticket to a group you edit; unnamed = your first group.
  const groups = useScopeGroups(open);

  // Assignee options (#171). Admin sees all users; non-admin sees self only.
  useEffect(() => {
    if (!open) return;
    fetch('/api/registry?path=/api/v1/users')
      .then(res => (res.ok ? res.json() : []))
      .then((d) => setUsers(Array.isArray(d) ? d.map((u: { username: string }) => u.username) : []))
      .catch(() => {});
  }, [open]);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setSuccess(false);

    const form = e.currentTarget;
    const formData = new FormData(form);

    // `source` is bearer-derived by the registry (extra fields are forbidden →
    // 422). The proxy forwards the session token, so the ticket is filed as the
    // logged-in user.
    const scope = (formData.get('group_scope') as string) || '';
    const payload: Record<string, unknown> = {
      summary: formData.get('summary') as string,
      type: formData.get('type') as string,
      priority: formData.get('priority') as string,
      body: (formData.get('body') as string) || null,
      assignee: (formData.get('assignee') as string) || null,
    };
    if (scope) payload.group_scope = scope;

    try {
      const res = await fetch('/api/registry?path=/api/v1/tickets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Failed: ${res.status}`);
      }

      setSuccess(true);
      form.reset();
      // Reload to show new ticket
      setTimeout(() => {
        window.location.reload();
      }, 1000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create ticket');
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-2.5 text-sm text-zinc-400 hover:text-white hover:border-zinc-700 transition-colors w-full text-left"
      >
        + New ticket...
      </button>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4"
    >
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-zinc-200">New Ticket</h4>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
        >
          Cancel
        </button>
      </div>

      <div className="space-y-3">
        <input
          name="summary"
          required
          placeholder="Summary"
          className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
        />

        <div className="flex gap-3">
          <select
            name="type"
            defaultValue="bug"
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
          >
            <option value="bug">Bug</option>
            <option value="feature">Feature</option>
            <option value="task">Task</option>
            <option value="incident">Incident</option>
          </select>
          <select
            name="priority"
            defaultValue="medium"
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
          >
            <option value="critical">Critical</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
          <select
            name="assignee"
            defaultValue=""
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
          >
            <option value="">Unassigned</option>
            {users.map(u => <option key={u} value={u}>{u}</option>)}
          </select>
          {/* scope v1: a group you edit; unnamed = your first group */}
          <select
            name="group_scope"
            defaultValue=""
            title="Who can see this ticket"
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
          >
            <option value="">my group (default)</option>
            {groups.map(g => <option key={g} value={g}>group: {g}</option>)}
          </select>
        </div>

        <textarea
          name="body"
          rows={3}
          placeholder="Details (optional)"
          className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none resize-none"
        />
      </div>

      {error && (
        <p className="text-xs text-red-400">{error}</p>
      )}
      {success && (
        <p className="text-xs text-emerald-400">Ticket created. Refreshing...</p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-100 hover:bg-zinc-700 transition-colors disabled:opacity-50"
      >
        {submitting ? 'Creating...' : 'Create Ticket'}
      </button>
    </form>
  );
}
