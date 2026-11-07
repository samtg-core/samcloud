'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * The thread this console conversation is working in (#886). Everything you
 * create here joins it. "New thread" is a new chat: the next thing you create
 * starts a thread. Picking one from the list resumes it. Sky when a thread is
 * current, so it is always visible where a create will land.
 */

interface Conversation {
  conversationId: string;
  threadId: number | null;
  threadTitle: string | null;
}

interface ThreadRow {
  id: number;
  title: string | null;
  state: string;
  open_members: boolean;
}

export function ThreadPill() {
  const [conv, setConv] = useState<Conversation | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [threads, setThreads] = useState<ThreadRow[]>([]);
  const ref = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const r = await fetch('/api/conversation', { cache: 'no-store' });
    if (!r.ok) { setConv(null); return; }
    const c: Conversation = await r.json();
    setConv(c);
    if (c.threadId) {
      const t = await fetch(`/api/registry?path=/api/v1/threads/${c.threadId}`, { cache: 'no-store' });
      setTitle(t.ok ? ((await t.json()).title ?? c.threadTitle) : c.threadTitle);
    } else {
      setTitle(null);
    }
  }, []);

  useEffect(() => {
    load();
    const onFocus = () => load();
    window.addEventListener('focus', onFocus);
    const iv = setInterval(load, 20000);
    return () => { window.removeEventListener('focus', onFocus); clearInterval(iv); };
  }, [load]);

  useEffect(() => {
    if (!open) return;
    fetch('/api/registry?path=/api/v1/threads', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : { threads: [] }))
      .then((d) => setThreads((d.threads || []).filter((t: ThreadRow) => t.state === 'open').slice(0, 15)))
      .catch(() => setThreads([]));
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const act = async (body: object) => {
    await fetch('/api/conversation', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    setOpen(false);
    load();
  };

  if (!conv) return null;
  const label = conv.threadId ? `#${conv.threadId}${title ? ` ${title}` : ''}` : 'new thread';
  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen((o) => !o)}
        title="The thread what you create here joins. New thread = a new chat; or resume one."
        className={`rounded-full border px-2 py-0.5 text-xs bg-zinc-900 max-w-28 sm:max-w-56 truncate transition-colors ${
          conv.threadId ? 'border-sky-500/50 text-sky-400' : 'border-zinc-800 text-zinc-500 hover:text-zinc-300'
        }`}
      >
        {label}
      </button>
      {open && (
        <div className="fixed right-4 top-14 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-zinc-800 bg-zinc-900 shadow-xl z-50 text-xs">
          <button
            onClick={() => act({ action: 'new' })}
            className="block w-full text-left px-3 py-2 text-zinc-200 hover:bg-zinc-800 border-b border-zinc-800"
          >
            + New thread <span className="text-zinc-500">— the next thing you create starts it</span>
          </button>
          <div className="px-3 pt-2 pb-1 text-zinc-500">Resume</div>
          <div className="max-h-72 overflow-y-auto">
            {threads.length === 0 && <div className="px-3 py-2 text-zinc-600">no open threads</div>}
            {threads.map((t) => (
              <button
                key={t.id}
                onClick={() => act({ action: 'resume', threadId: t.id, title: t.title })}
                className={`block w-full text-left px-3 py-1.5 hover:bg-zinc-800 truncate ${
                  t.id === conv.threadId ? 'text-sky-400' : 'text-zinc-300'
                }`}
              >
                #{t.id} {t.title}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
