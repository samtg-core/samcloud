'use client';

import { useEffect, useState } from 'react';

/**
 * Groups the logged-in user may scope content to.
 *
 * Deriving this from `auth/verify` scopes alone breaks for admins: their
 * scope is the wildcard `["*"]`, which contains no `group:` entries, so the
 * scope dropdowns rendered nothing but commons — found by driving the real
 * UI as an admin (#328; code-reading had missed it because the derivation
 * looks perfectly sensible for every non-admin token).
 */
export function useScopeGroups(enabled = true): string[] {
  const [groups, setGroups] = useState<string[]>([]);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    (async () => {
      try {
        const v = await fetch('/api/registry?path=/api/v1/auth/verify', { cache: 'no-store' })
          .then(r => (r.ok ? r.json() : { scopes: [] }));
        const scopes: string[] = v.scopes || [];
        if (scopes.includes('*')) {
          const all = await fetch('/api/registry?path=/api/v1/groups', { cache: 'no-store' })
            .then(r => (r.ok ? r.json() : []));
          if (alive) setGroups(Array.isArray(all) ? all.map((g: { id: string }) => g.id).sort() : []);
        } else {
          if (alive) setGroups(scopes.filter(s => s.startsWith('group:')).map(s => s.slice('group:'.length)).sort());
        }
      } catch {
        if (alive) setGroups([]);
      }
    })();
    return () => { alive = false; };
  }, [enabled]);

  return groups;
}
