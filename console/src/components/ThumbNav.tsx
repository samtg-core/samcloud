'use client';

import { useState, useEffect } from 'react';
import { usePathname } from 'next/navigation';
import { NewTicketModal } from '@/components/NewTicketModal';

/**
 * Floating thumb-reach navigation cluster (#327, refined #332): a burger
 * with quick-create shortcuts, always mounted (in the root layout) so it
 * shows on every page, plus block-step arrows that only appear where the
 * page actually has [data-block] elements to step through.
 *
 * "Blocks" are whatever the page marks with [data-block] — comments,
 * sections. Single click steps to the next/previous block; double-click
 * jumps to page top/bottom. The distinction matters: stepping is for
 * reading a long thread, the double-tap is for escaping one.
 */
export function ThumbNav() {
  const [menu, setMenu] = useState(false);
  const [newTicket, setNewTicket] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);
  const [hasBlocks, setHasBlocks] = useState(false);
  const [atTop, setAtTop] = useState(true);
  const [unread, setUnread] = useState(0);
  const pathname = usePathname();

  useEffect(() => {
    fetch('/api/auth/login')
      .then(r => setLoggedIn(r.ok))
      .catch(() => setLoggedIn(false));
  }, []);

  // Same source InboxBadge polls — surfaced here too (#332) so the notif
  // count is visible from the always-on burger, not just the header.
  useEffect(() => {
    if (!loggedIn) return;
    let alive = true;
    async function poll() {
      try {
        const res = await fetch(`/api/registry?path=${encodeURIComponent('/api/v1/users/me/briefing')}`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (alive && typeof data.unread_total === 'number') setUnread(data.unread_total);
      } catch { /* signed out or registry down */ }
    }
    poll();
    const id = setInterval(poll, 60000);
    return () => { alive = false; clearInterval(id); };
  }, [loggedIn]);

  // Content (comments, sections) loads client-side after navigation, so a
  // one-shot check on pathname change isn't enough — watch the DOM.
  useEffect(() => {
    const check = () => setHasBlocks(document.querySelectorAll('[data-block]').length > 0);
    check();
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [pathname]);

  // Near the top only the down caret shows (#332: you can't go up from the top).
  useEffect(() => {
    const onScroll = () => setAtTop(window.scrollY < 40);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  if (!loggedIn) return null;

  function blocks(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>('[data-block]'));
  }

  function step(dir: 1 | -1) {
    const bs = blocks();
    if (!bs.length) return;
    const y = window.scrollY + 90; // account for header
    // next block strictly past the viewport anchor in the travel direction
    const target = dir === 1
      ? bs.find(b => b.offsetTop > y + 10)
      : [...bs].reverse().find(b => b.offsetTop < y - 10);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    else window.scrollTo({ top: dir === 1 ? document.body.scrollHeight : 0, behavior: 'smooth' });
  }

  const burgerBtn = 'relative w-9 h-9 flex items-center justify-center rounded-full border border-zinc-700 bg-zinc-900/90 text-zinc-300 hover:text-white hover:border-zinc-500 backdrop-blur text-sm select-none';
  // Tiny caret glyph — thin chevron, no chrome (#332: "very small up and
  // down carets", not the old big blue capsule).
  const caretBtn = 'w-6 h-6 flex items-center justify-center text-zinc-400 hover:text-white select-none';
  const Chevron = ({ up }: { up?: boolean }) => (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <polyline points={up ? '6 15 12 9 18 15' : '6 9 12 15 18 9'} />
    </svg>
  );

  return (
    <>
      {/* Everything anchored bottom-right.
          The block-step carets sit just to the RIGHT of the burger — same tiny
          chevron size/profile as approved, now beside the burger rather
          than up by the header (#332 final). Menu still expands above. */}
      <div className="fixed bottom-3 right-3 z-50 flex flex-col items-end gap-2">
        {menu && (
          <div className="flex flex-col gap-1 rounded-lg border border-zinc-700 bg-zinc-900/95 backdrop-blur p-2 text-sm shadow-xl">
            <a href="/search" className="px-3 py-1.5 rounded text-zinc-300 hover:bg-zinc-800 hover:text-white whitespace-nowrap">Search</a>
            <button
              onClick={() => { setMenu(false); setNewTicket(true); }}
              className="px-3 py-1.5 rounded text-zinc-300 hover:bg-zinc-800 hover:text-white whitespace-nowrap text-left"
            >+ New ticket</button>
            <a href="/docs" className="px-3 py-1.5 rounded text-zinc-300 hover:bg-zinc-800 hover:text-white whitespace-nowrap">+ New doc</a>
            <a href="/projects" className="px-3 py-1.5 rounded text-zinc-300 hover:bg-zinc-800 hover:text-white whitespace-nowrap">+ New task</a>
            <a href="/files" className="px-3 py-1.5 rounded text-zinc-300 hover:bg-zinc-800 hover:text-white whitespace-nowrap">Upload file</a>
            <a href="/inbox" className="px-3 py-1.5 rounded text-zinc-300 hover:bg-zinc-800 hover:text-white whitespace-nowrap flex items-center justify-between gap-3">
              <span>Jump to inbox</span>
              {unread > 0 && (
                <span className="inline-flex items-center justify-center rounded-full bg-blue-500/20 border border-blue-500/40 text-blue-300 text-[10px] leading-none px-1.5 py-0.5 min-w-[1.25rem]">
                  {unread > 99 ? '99+' : unread}
                </span>
              )}
            </a>
          </div>
        )}

        {/* Row: burger, then the caret pair to its right (at the corner). */}
        <div className="flex items-end gap-1.5">
          <button aria-label="quick create" className={burgerBtn} onClick={() => setMenu(m => !m)}>
            ≡
            {unread > 0 && (
              <span className="absolute -top-1 -right-1 inline-flex items-center justify-center rounded-full bg-blue-500 border border-zinc-950 text-white text-[9px] leading-none px-1 py-0.5 min-w-[1rem]">
                {unread > 99 ? '99+' : unread}
              </span>
            )}
          </button>
          {hasBlocks && (
            <div className="flex flex-col items-center rounded-md bg-zinc-900/70 backdrop-blur border border-zinc-800/80">
              {!atTop && (
                <button
                  aria-label="previous block (double-click: top)"
                  title="up a block · double-click: top"
                  className={caretBtn}
                  onClick={() => step(-1)}
                  onDoubleClick={(e) => { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }); }}
                ><Chevron up /></button>
              )}
              <button
                aria-label="next block (double-click: bottom)"
                title="down a block · double-click: bottom"
                className={caretBtn}
                onClick={() => step(1)}
                onDoubleClick={(e) => { e.preventDefault(); window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); }}
              ><Chevron /></button>
            </div>
          )}
        </div>
      </div>

      {newTicket && <NewTicketModal onClose={() => setNewTicket(false)} />}
    </>
  );
}
