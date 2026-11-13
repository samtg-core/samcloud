'use client';

import { useState, useEffect } from 'react';

/**
 * Ticket creation as an actual modal (#332) — the burger's "New ticket"
 * shortcut used to just navigate to /tickets and rely on its inline form.
 * Same payload shape as app/tickets/CreateTicketForm.tsx. On success it
 * stays in place (#376): a success panel with a link to the new ticket,
 * so filing a ticket never yanks you off the page you were reading.
 */
export function NewTicketModal({ onClose }: { onClose: () => void }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [users, setUsers] = useState<string[]>([]);
  const [created, setCreated] = useState<{ id: number; summary: string } | null>(null);

  useEffect(() => {
    fetch('/api/registry?path=/api/v1/users')
      .then(res => (res.ok ? res.json() : []))
      .then((d) => setUsers(Array.isArray(d) ? d.map((u: { username: string }) => u.username) : []))
      .catch(() => {});
  }, []);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);

    const formData = new FormData(e.currentTarget);
    const payload = {
      summary: formData.get('summary') as string,
      type: formData.get('type') as string,
      priority: formData.get('priority') as string,
      body: (formData.get('body') as string) || null,
      assignee: (formData.get('assignee') as string) || null,
    };

    try {
      const res = await fetch('/api/registry?path=/api/v1/tickets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Failed: ${res.status}`);
      setCreated({ id: data.ticket_id, summary: payload.summary });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create ticket');
    } finally {
      setSubmitting(false);
    }
  }

  if (created) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
        <div
          onClick={(e) => e.stopPropagation()}
          className="w-full max-w-lg rounded-lg border border-zinc-800 bg-zinc-900 shadow-2xl p-6 space-y-4"
        >
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-400 text-sm">✓</span>
            <h2 className="text-lg font-semibold text-zinc-100">Ticket created</h2>
          </div>
          <a
            href={`/tickets/${created.id}`}
            className="block rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-blue-400 hover:underline truncate"
          >
            #{created.id} — {created.summary}
          </a>
          <div className="flex justify-end gap-3">
            <button
              type="button"
              onClick={() => setCreated(null)}
              className="rounded-lg px-4 py-2 text-sm text-zinc-400 hover:text-white transition-colors"
            >
              Create another
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg bg-zinc-700 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-600 transition-colors"
            >
              Done
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <form
        onSubmit={handleSubmit}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg rounded-lg border border-zinc-800 bg-zinc-900 shadow-2xl p-6 space-y-4"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-zinc-100">New Ticket</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-300 transition-colors text-lg leading-none"
          >
            x
          </button>
        </div>

        <div className="space-y-3">
          <input
            name="summary"
            required
            autoFocus
            placeholder="Summary"
            className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
          />

          {/* Two per row on a phone: a select is as wide as its longest option,
              so one long assignee list ran off the screen (#1082). */}
          <div className="grid grid-cols-2 gap-3 sm:flex">
            <select
              name="type"
              defaultValue="bug"
              className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
            >
              <option value="bug">Bug</option>
              <option value="feature">Feature</option>
              <option value="task">Task</option>
              <option value="incident">Incident</option>
            </select>
            <select
              name="priority"
              defaultValue="medium"
              className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
            >
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
            <select
              name="assignee"
              defaultValue=""
              className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:border-zinc-600 focus:outline-none"
            >
              <option value="">Unassigned</option>
              {users.map(u => <option key={u} value={u}>{u}</option>)}
            </select>
          </div>

          <textarea
            name="body"
            rows={3}
            placeholder="Details (optional)"
            className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-zinc-600 focus:outline-none resize-none"
          />
        </div>

        {error && <p className="text-xs text-red-400">{error}</p>}

        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-zinc-400 hover:text-white transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 transition-colors disabled:opacity-50"
          >
            {submitting ? 'Creating...' : 'Create Ticket'}
          </button>
        </div>
      </form>
    </div>
  );
}
