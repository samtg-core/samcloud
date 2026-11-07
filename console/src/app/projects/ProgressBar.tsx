'use client';

export function ProgressBar({ pct, basis }: { pct: number; basis?: string }) {
  return (
    <div className="flex items-center gap-2 min-w-0" title={basis ? `progress (${basis})` : 'progress'}>
      <div className="h-1.5 flex-1 min-w-16 rounded-full bg-zinc-800 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${pct >= 100 ? 'bg-blue-500' : 'bg-emerald-500'}`}
          style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
        />
      </div>
      <span className="text-xs text-zinc-500 tabular-nums w-9 text-right">{pct}%</span>
    </div>
  );
}
