'use client';

import { useEffect, useState } from 'react';

/**
 * The inbox reading queue (#391): opening a notification from /inbox stashes
 * the rest of the list, and this floating pill walks you through them —
 * "next →" from wherever you landed, no round-trip to the inbox between
 * items. sessionStorage-backed, so it survives full-page navigations and
 * dies with the tab. Hidden on /inbox itself (the list IS the queue there).
 */

const KEY = 'samcloud-inbox-queue';

interface Queue {
  items: { href: string; label: string }[];
  idx: number;
}

export function InboxQueuePill() {
  const [q, setQ] = useState<Queue | null>(null);

  useEffect(() => {
    if (window.location.pathname === '/inbox') return;
    try {
      const raw = sessionStorage.getItem(KEY);
      if (raw) setQ(JSON.parse(raw));
    } catch { /* corrupted stash — ignore */ }
  }, []);

  if (!q || !Array.isArray(q.items) || q.idx >= q.items.length) return null;
  const next = q.items[q.idx];
  const prev = q.idx > 0 ? q.items[q.idx - 1] : null;
  const remaining = q.items.length - q.idx;

  return (
    <div className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900/95 shadow-xl px-3 py-2 text-xs backdrop-blur">
      <span className="text-zinc-500 whitespace-nowrap">inbox · {remaining} left</span>
      {prev && (
        <a
          href={prev.href}
          onClick={() => {
            try { sessionStorage.setItem(KEY, JSON.stringify({ ...q, idx: q.idx - 1 })); } catch { /* ignore */ }
          }}
          className="text-zinc-400 hover:text-white whitespace-nowrap"
          title={prev.label}
        >
          ← back
        </a>
      )}
      <a
        href={next.href}
        onClick={() => {
          try { sessionStorage.setItem(KEY, JSON.stringify({ ...q, idx: q.idx + 1 })); } catch { /* full/blocked storage */ }
        }}
        className="text-blue-400 hover:underline max-w-48 truncate"
        title={next.label}
      >
        next: {next.label} →
      </a>
      <button
        onClick={() => {
          try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
          setQ(null);
        }}
        className="text-zinc-600 hover:text-zinc-300"
        title="dismiss queue"
      >
        ×
      </button>
    </div>
  );
}

/** Called by the inbox when a notification link is opened: everything else
 *  still on screen becomes the walk-through queue. */
export function stashInboxQueue(items: { href: string; label: string }[]) {
  try {
    if (items.length) sessionStorage.setItem(KEY, JSON.stringify({ items, idx: 0 }));
    else sessionStorage.removeItem(KEY);
  } catch { /* ignore */ }
}
