'use client';

import { useState, useEffect, useCallback } from 'react';
import { useEvents } from '@/hooks/useEvents';

interface Device {
  id: string;
  name: string;
  status: string;
}

interface Service {
  id: string;
  name: string;
  device_id: string;
  port: number | null;
  slice_port: number | null;
  protocol: string;
  health_endpoint: string | null;
  subdomain: string | null;
  domain: string | null;
  external_url: string | null;
  status: string;
  last_health: string | null;
}

interface SlotResult {
  service_id: string;
  token: string;
  subdomain: string;
  brief_url: string;
}

const PROXY = '/api/registry';

function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
      className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

export default function ServicesPage() {
  const [services, setServices] = useState<Service[]>([]);
  const [pending, setPending] = useState<Service[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Slot form
  const [slotName, setSlotName] = useState('');
  const [slotDevice, setSlotDevice] = useState('');
  const [slotDesc, setSlotDesc] = useState('');
  const [creating, setCreating] = useState(false);
  const [slotResult, setSlotResult] = useState<SlotResult | null>(null);
  const [slotError, setSlotError] = useState('');
  const { subscribe, connected } = useEvents();

  const loadData = useCallback(async () => {
    try {
      const [svcRes, devRes, pendRes] = await Promise.all([
        fetch(`${PROXY}?path=/api/v1/services`),
        fetch(`${PROXY}?path=/api/v1/devices`),
        fetch(`${PROXY}?path=/api/v1/enroll/services/pending`),
      ]);
      if (svcRes.ok) setServices(await svcRes.json());
      if (devRes.ok) setDevices(await devRes.json());
      if (pendRes.ok) setPending(await pendRes.json());
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Live updates: re-fetch when services or health events arrive
  useEffect(() => {
    const unsub1 = subscribe('service.*', () => loadData());
    const unsub2 = subscribe('health.*', () => loadData());
    return () => { unsub1(); unsub2(); };
  }, [subscribe, loadData]);

  async function handleCreateSlot(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setSlotResult(null);
    setSlotError('');

    try {
      const res = await fetch(`${PROXY}?path=/api/v1/services/enroll`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: slotName,
          device_id: slotDevice,
          description: slotDesc || undefined,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setSlotResult(data);
        setSlotName('');
        setSlotDevice('');
        setSlotDesc('');
        loadData();
      } else {
        setSlotError(data.detail || 'Failed to create slot');
      }
    } catch {
      setSlotError('Error connecting to registry');
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(serviceId: string) {
    if (!confirm(`Delete service "${serviceId}"?`)) return;
    const res = await fetch(`${PROXY}?path=/api/v1/services/${encodeURIComponent(serviceId)}`, { method: 'DELETE' });
    if (res.ok) loadData();
  }

  if (loading) return <div className="max-w-6xl mx-auto"><p className="text-zinc-500">Loading...</p></div>;
  if (error) return <div className="max-w-6xl mx-auto"><div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center"><p className="text-red-400">{error}</p></div></div>;

  const active = services.filter(s => s.status !== 'pending');

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <div>
        <div className="flex items-center gap-3 mb-1">
          <a href="/admin" className="text-sm text-zinc-500 hover:text-zinc-300">Admin</a>
          <span className="text-zinc-700">/</span>
          <h2 className="text-2xl font-bold text-zinc-100">Services</h2>
          {connected && <span className="inline-flex items-center gap-1 text-xs text-emerald-500"><span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />live</span>}
        </div>
        <p className="text-zinc-500 mt-1">Create service slots and manage registrations</p>
      </div>

      {/* Create Service Slot */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">Create Service Slot</h3>
        <p className="text-xs text-zinc-500">Define what should exist. Give the token to the agent — they fill in the runtime details.</p>

        <form onSubmit={handleCreateSlot} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <input type="text" placeholder="Service name *" value={slotName} onChange={e => setSlotName(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" required />
            <select value={slotDevice} onChange={e => setSlotDevice(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none" required>
              <option value="">Device...</option>
              {devices.filter(d => d.status === 'online').map(d => (
                <option key={d.id} value={d.id}>{d.id}</option>
              ))}
            </select>
            <input type="text" placeholder="Description (optional)" value={slotDesc} onChange={e => setSlotDesc(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />
          </div>
          <div className="flex items-center gap-3">
            <button type="submit" disabled={creating}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors">
              {creating ? 'Creating...' : 'Create Slot'}
            </button>
            {slotError && <p className="text-xs text-red-400">{slotError}</p>}
          </div>
        </form>

        {/* Slot Result — token for agent */}
        {slotResult && (
          <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4 space-y-3">
            <h4 className="text-sm font-medium text-emerald-400">Service Slot Created</h4>
            <p className="text-xs text-zinc-400">
              Give this token to the agent managing <strong className="text-zinc-200">{slotResult.service_id}</strong>.
            </p>

            <div className="rounded-lg border border-zinc-800 bg-zinc-900/80 p-3 space-y-3">
              <div>
                <span className="text-xs text-zinc-500">Give this to the agent (briefing + instructions):</span>
                <div className="flex items-center gap-2 mt-1">
                  <code className="flex-1 text-sm font-mono text-zinc-200 break-all">
                    {slotResult.brief_url || `${typeof window !== 'undefined' ? window.location.origin : ''}/api/v1/services/brief/${slotResult.token}`}
                  </code>
                  <CopyBtn text={slotResult.brief_url || `${typeof window !== 'undefined' ? window.location.origin : ''}/api/v1/services/brief/${slotResult.token}`} />
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Pending Slots */}
      {pending.length > 0 && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold text-zinc-100">Pending Activation</h3>
          <div className="space-y-2">
            {pending.map(s => (
              <div key={s.id} className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4 flex items-center justify-between">
                <div>
                  <span className="font-mono text-zinc-200">{s.name}</span>
                  <span className="text-zinc-500 text-xs ml-2">on {s.device_id}</span>
                  {s.subdomain && <span className="text-zinc-600 text-xs ml-2">{s.subdomain}</span>}
                </div>
                <div className="flex gap-2">
                  <StatusBadge status="pending" />
                  <button onClick={() => handleDelete(s.id)}
                    className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 transition-colors">Delete</button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Active Services */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">Services ({active.length})</h3>
        {active.length > 0 ? (
          <div className="space-y-2">
            {active.map(svc => (
              <div key={svc.id} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                  <span className="font-mono text-zinc-200 font-medium">{svc.name}</span>
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="text-xs text-zinc-500">{svc.device_id}</span>
                    <StatusBadge status={svc.status} lastHealth={svc.last_health} />
                    <button onClick={() => handleDelete(svc.id)}
                      className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 transition-colors">Delete</button>
                  </div>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                  <div><span className="text-zinc-600">Port: </span><span className="font-mono text-zinc-400">{svc.port || 'pending'}</span></div>
                  {svc.slice_port && <div><span className="text-zinc-600">Forward: </span><span className="font-mono text-zinc-400">:{svc.slice_port}</span></div>}
                  {svc.health_endpoint && <div><span className="text-zinc-600">Health: </span><span className="font-mono text-zinc-400">{svc.health_endpoint}</span></div>}
                  {svc.external_url && <div><span className="text-zinc-600">URL: </span><a href={svc.external_url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300">{svc.external_url.replace('https://', '')}</a></div>}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">No active services</div>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status, lastHealth }: { status: string; lastHealth?: string | null }) {
  // Derive display status from health staleness
  let displayStatus = status;
  if (status === 'online' && lastHealth) {
    const age = (Date.now() - new Date(lastHealth).getTime()) / 1000;
    if (age > 300) displayStatus = 'offline';      // >5 min
    else if (age > 120) displayStatus = 'stale';    // >2 min
  }

  const styles: Record<string, string> = {
    online: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    active: 'bg-yellow-500/10 border-yellow-500/20 text-yellow-400',
    stale: 'bg-orange-500/10 border-orange-500/20 text-orange-400',
    pending: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
    offline: 'bg-red-500/10 border-red-500/20 text-red-400',
    stopped: 'bg-red-500/10 border-red-500/20 text-red-400',
    degraded: 'bg-orange-500/10 border-orange-500/20 text-orange-400',
    maintenance: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
    unknown: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${styles[displayStatus] || styles.unknown}`}>
      {displayStatus}
    </span>
  );
}
