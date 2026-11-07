'use client';

// Nav link to /inbox with a live unread count (#203). Polls the briefing
// endpoint via the auth proxy; renders nothing special when signed out or
// at inbox zero — the badge only exists when there is something to see.

import { useEffect, useState } from 'react';

export default function InboxBadge() {
  const [unread, setUnread] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const res = await fetch(`/api/registry?path=${encodeURIComponent('/api/v1/users/me/briefing')}`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (alive && typeof data.unread_total === 'number') setUnread(data.unread_total);
      } catch {
        /* signed out or registry down — no badge */
      }
    }
    poll();
    const id = setInterval(poll, 60000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return (
    <a href="/inbox" className="text-zinc-400 hover:text-white whitespace-nowrap flex items-center gap-1.5">
      Inbox
      {unread !== null && unread > 0 && (
        <span className="inline-flex items-center justify-center rounded-full bg-blue-500/20 border border-blue-500/40 text-blue-300 text-[10px] leading-none px-1.5 py-0.5 min-w-[1.25rem]">
          {unread > 99 ? '99+' : unread}
        </span>
      )}
    </a>
  );
}
