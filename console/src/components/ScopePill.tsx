'use client';

import { useScopeGroups } from '@/lib/useScopeGroups';
import { useScopeFilter, setScopeFilter } from '@/lib/scopeFilter';

/**
 * The top-level scope switch (H2, #380): pick a group and the list pages
 * (tickets/docs/projects) show only that plane. Amber when active so a
 * narrowed console never masquerades as the full picture.
 */
export function ScopePill() {
  const groups = useScopeGroups();
  const scope = useScopeFilter();
  if (!groups.length) return null;
  return (
    <select
      value={scope}
      onChange={(e) => setScopeFilter(e.target.value)}
      title="Scope filter — show only one group's tickets, docs and projects"
      className={`rounded-full border px-2 py-0.5 text-xs bg-zinc-900 focus:outline-none cursor-pointer transition-colors max-w-28 sm:max-w-none ${
        scope
          ? 'border-amber-500/50 text-amber-400'
          : 'border-zinc-800 text-zinc-500 hover:text-zinc-300'
      }`}
    >
      <option value="">all scopes</option>
      {groups.map((g) => (
        <option key={g} value={g}>{g}</option>
      ))}
    </select>
  );
}
