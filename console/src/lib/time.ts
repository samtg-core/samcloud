// Shared time helpers (2026-08-16: fleet vitals showed fresh rows as hours old).
//
// Registry timestamps are SQLite datetime('now') — UTC, no timezone marker
// ("2026-08-16 09:05:31"). JS Date() parses unmarked strings as LOCAL time,
// which shifted everything by the UTC offset. The inbox page fixed this
// locally in v0.7.8x; tickets/docs kept stale copies. One helper, one rule:
// no marker → it's UTC, append Z.

export function parseRegistryDate(iso: string): Date {
  if (!iso) return new Date(NaN);
  const marked = iso.endsWith('Z') || iso.includes('+') || /T.*-\d\d:?\d\d$/.test(iso);
  return new Date(marked ? iso : iso.replace(' ', 'T') + 'Z');
}

export function timeAgo(iso: string): string {
  const diffMin = Math.floor((Date.now() - parseRegistryDate(iso).getTime()) / 60000);
  if (Number.isNaN(diffMin)) return '';
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

// Local-formatted absolute time for detail pages (comment headers, footers).
export function fmtLocal(iso: string): string {
  const d = parseRegistryDate(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}
