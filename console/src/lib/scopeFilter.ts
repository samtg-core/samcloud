'use client';

import { useEffect, useState } from 'react';

/**
 * Global scope filter (H2, #380): one top-level switch that narrows tickets,
 * docs and projects to a single group (e.g. 'work'). Persisted per-browser in
 * localStorage; a custom event keeps every consumer in sync so the nav pill
 * and the list pages never disagree. Empty string = all scopes (off).
 */

const KEY = 'samcloud-scope-filter';
const EVT = 'samcloud-scope-change';

export function getScopeFilter(): string {
  if (typeof window === 'undefined') return '';
  return localStorage.getItem(KEY) || '';
}

export function setScopeFilter(scope: string) {
  if (scope) localStorage.setItem(KEY, scope);
  else localStorage.removeItem(KEY);
  window.dispatchEvent(new CustomEvent(EVT));
}

export function useScopeFilter(): string {
  const [scope, setScope] = useState('');
  useEffect(() => {
    setScope(getScopeFilter());
    const on = () => setScope(getScopeFilter());
    window.addEventListener(EVT, on);
    window.addEventListener('storage', on);   // other tabs
    return () => {
      window.removeEventListener(EVT, on);
      window.removeEventListener('storage', on);
    };
  }, []);
  return scope;
}
