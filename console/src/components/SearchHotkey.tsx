'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

/**
 * Search from anywhere (#1082): ⌘K / Ctrl-K, or "/" when you are not typing,
 * opens /search with the box focused. Rendered in the header as the search icon,
 * so a phone gets a tap target and a keyboard gets the shortcut.
 */
export function SearchHotkey() {
  const router = useRouter();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      const typing = !!el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName));
      if ((e.key === 'k' && (e.metaKey || e.ctrlKey)) || (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey)) {
        e.preventDefault();
        router.push('/search');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [router]);
  return (
    <a href="/search" title="Search (⌘K or /)" aria-label="Search"
       className="flex shrink-0 items-center text-zinc-400 hover:text-white">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
           strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <circle cx="11" cy="11" r="7" /><line x1="20" y1="20" x2="16.2" y2="16.2" />
      </svg>
    </a>
  );
}
