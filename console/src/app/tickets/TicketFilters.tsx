'use client';

const STATUSES = ['open', 'triaged', 'in_progress', 'resolved'];

export function TicketFilters({ currentStatus }: { currentStatus?: string }) {
  return (
    <div className="flex flex-wrap gap-2">
      <a
        href="/tickets"
        className={`rounded-full border px-3 py-1 text-xs transition-colors ${
          !currentStatus
            ? 'border-zinc-500 bg-zinc-800 text-zinc-100'
            : 'border-zinc-800 bg-zinc-900/50 text-zinc-400 hover:text-white hover:border-zinc-700'
        }`}
      >
        All
      </a>
      {STATUSES.map((status) => (
        <a
          key={status}
          href={`/tickets?status=${status}`}
          className={`rounded-full border px-3 py-1 text-xs transition-colors ${
            currentStatus === status
              ? 'border-zinc-500 bg-zinc-800 text-zinc-100'
              : 'border-zinc-800 bg-zinc-900/50 text-zinc-400 hover:text-white hover:border-zinc-700'
          }`}
        >
          {status.replace('_', ' ')}
        </a>
      ))}
    </div>
  );
}
