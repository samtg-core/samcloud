'use client';

import { useState, useEffect, useCallback } from 'react';
import { useEvents } from '@/hooks/useEvents';

interface DashboardResource {
  id: string;
  label: string;
  device: string;
  type: string;
  provider: string;
  health: string;
  memory_pct: number;
  compute_pct: number;
  temperature_c: number | null;
  power_draw_w: number | null;
  leases: number;
  available_memory_mb: number;
  last_stats: string;
}

const PROXY = '/api/registry';

// #136: registry payloads can carry missing/null/negative numerics for
// never-reported resources — render '—' instead of NaN/undefined.
const fmtPct = (v: unknown) =>
  Number.isFinite(v as number) ? `${(v as number).toFixed(1)}%` : '—';
const fmtMem = (mb: unknown) =>
  !Number.isFinite(mb as number) || (mb as number) < 0 ? '—'
    : (mb as number) >= 1024 ? `${((mb as number) / 1024).toFixed(1)}GB` : `${mb}MB`;

function HealthDot({ health }: { health: string }) {
  const colors: Record<string, string> = {
    green: 'bg-emerald-500',
    amber: 'bg-amber-500',
    red: 'bg-red-500',
    grey: 'bg-zinc-500',
  };
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${colors[health] || colors.grey}`} />;
}

function UsageBar({ pct, color }: { pct: number; color: string }) {
  const safe = Number.isFinite(pct) ? Math.max(0, Math.min(pct, 100)) : 0;
  return (
    <div className="w-full h-2 rounded-full bg-zinc-800 overflow-hidden">
      <div className={`h-full rounded-full ${color}`} style={{ width: `${safe}%` }} />
    </div>
  );
}

export default function ResourcesPage() {
  const [resources, setResources] = useState<DashboardResource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const { subscribe, connected } = useEvents();

  const loadData = useCallback(async () => {
    try {
      const res = await fetch(`${PROXY}?path=/api/v1/resources/dashboard`);
      if (res.ok) setResources(await res.json());
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Live updates on resource events
  useEffect(() => {
    const unsub1 = subscribe('resource.*', () => loadData());
    const unsub2 = subscribe('lease.*', () => loadData());
    return () => { unsub1(); unsub2(); };
  }, [subscribe, loadData]);

  if (loading) return <div className="max-w-6xl mx-auto"><p className="text-zinc-500">Loading...</p></div>;
  if (error) return <div className="max-w-6xl mx-auto"><div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center"><p className="text-red-400">{error}</p></div></div>;

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <div>
        <div className="flex items-center gap-3 mb-1">
          <a href="/admin" className="text-sm text-zinc-500 hover:text-zinc-300">Admin</a>
          <span className="text-zinc-700">/</span>
          <h2 className="text-2xl font-bold text-zinc-100">Resources</h2>
          {connected && <span className="inline-flex items-center gap-1 text-xs text-emerald-500"><span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />live</span>}
        </div>
        <p className="text-zinc-500 mt-1">Compute resources, utilisation, and leases</p>
      </div>

      {resources.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {resources.map(r => (
            <div key={r.id} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-5 space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <span className="font-mono text-zinc-200 font-medium">{r.label}</span>
                  <span className="text-xs text-zinc-500 ml-2">{r.provider}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-zinc-500">{r.device}</span>
                  <HealthDot health={r.health} />
                </div>
              </div>

              <div className="space-y-2">
                <div>
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-zinc-500">Memory</span>
                    <span className="text-zinc-400">{fmtPct(r.memory_pct)}</span>
                  </div>
                  <UsageBar pct={r.memory_pct} color={r.memory_pct > 85 ? 'bg-red-500' : r.memory_pct > 60 ? 'bg-amber-500' : 'bg-emerald-500'} />
                </div>
                <div>
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-zinc-500">Compute</span>
                    <span className="text-zinc-400">{fmtPct(r.compute_pct)}</span>
                  </div>
                  <UsageBar pct={r.compute_pct} color={r.compute_pct > 85 ? 'bg-red-500' : r.compute_pct > 60 ? 'bg-amber-500' : 'bg-emerald-500'} />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-2 text-xs">
                {r.temperature_c != null && (
                  <div><span className="text-zinc-600">Temp </span><span className="text-zinc-400">{r.temperature_c}°C</span></div>
                )}
                {r.power_draw_w != null && (
                  <div><span className="text-zinc-600">Power </span><span className="text-zinc-400">{r.power_draw_w}W</span></div>
                )}
                <div><span className="text-zinc-600">Leases </span><span className="text-zinc-400">{r.leases}</span></div>
                <div><span className="text-zinc-600">Free </span><span className="font-mono text-zinc-400">{fmtMem(r.available_memory_mb)}</span></div>
                <div><span className="text-zinc-600">Updated </span><span className="text-zinc-400">{r.last_stats}</span></div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
          No resources registered. Agents can register GPUs and compute resources via the API.
        </div>
      )}
    </div>
  );
}
