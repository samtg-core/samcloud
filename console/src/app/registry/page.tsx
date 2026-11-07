'use client';

import { timeAgo } from '@/lib/time';
import { useEffect, useState } from 'react';
import { StatCard } from '@/components/StatusCard';

// --- Types ---

interface Device {
  id: string;
  hostname: string;
  context: string;
  type: string;
  ip: string | null;
  ssh_user: string | null;
  ssh_port: number;
  tunnel_port: number | null;
  lifeline_port: number | null;
  monitor_port: number | null;
  enrolled: number;
  enrolled_at: string | null;
  startup_method: string | null;
  recovery_method: string | null;
  status: string;
}

interface Service {
  id: string;
  name: string;
  device_id: string;
  port: number;
  slice_port: number | null;
  protocol: string;
  health_endpoint: string | null;
  subdomain: string | null;
  domain: string | null;
  external_url: string | null;
  status: string;
  version: string | null;
  capabilities: string | null;
}

interface Port {
  port: number;
  type: string;
  owner_id: string;
  allocated_at: string | null;
}

interface Ticket {
  id: number;
  source: string;
  type: string;
  priority: string;
  status: string;
  summary: string;
  body: string | null;
  service_id: string | null;
  device_id: string | null;
  assignee: string | null;
  created_at: string;
  updated_at: string | null;
  resolved_at: string | null;
}

interface AuditEntry {
  id: number;
  actor: string;
  action: string;
  target: string | null;
  detail: string | null;
  created_at: string;
}

interface RegistryStatus {
  devices: Device[];
  services: Service[];
  ports: Port[];
  routes: { id: number; subdomain: string; domain: string; target: string; service_id: string | null; created_at: string }[];
  open_tickets: Ticket[];
  counts: {
    devices: number;
    services: number;
    ports: number;
    routes: number;
    open_tickets: number;
  };
}

// Port ranges from ports.py
const PORT_RANGES: Record<string, { lo: number; hi: number; label: string }> = {
  tunnel:   { lo: 10001, hi: 10012, label: 'Tunnel (SSH reverse)' },
  lifeline: { lo: 10013, hi: 10099, label: 'Lifeline (Windows)' },
  forward:  { lo: 18001, hi: 18099, label: 'Forward (service)' },
  local:    { lo: 3000,  hi: 3099,  label: 'Local (host web)' },
  monitor:  { lo: 19001, hi: 19099, label: 'Monitor (GPU/device)' },
};

// --- Data fetching ---
// Through the /api/registry proxy: it forwards the caller's session token and
// the registry enforces scope (fail closed). This page never hits the registry
// directly, so it can't leak the mesh to an unscoped session.

async function getRegistryStatus(): Promise<RegistryStatus | null> {
  try {
    const res = await fetch('/api/registry?path=/api/v1/status', { cache: 'no-store' });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

async function getAuditLog(): Promise<AuditEntry[]> {
  try {
    const res = await fetch(
      `/api/registry?path=${encodeURIComponent('/api/v1/audit?limit=10')}`,
      { cache: 'no-store' },
    );
    if (!res.ok) return [];
    return res.json();
  } catch {
    return [];
  }
}

// --- Helpers ---

function contextBadge(context: string) {
  if (context === 'tunnel') {
    return <span className="inline-flex items-center rounded-full bg-purple-500/10 border border-purple-500/20 px-2 py-0.5 text-xs text-purple-400">{context}</span>;
  }
  return <span className="inline-flex items-center rounded-full bg-blue-500/10 border border-blue-500/20 px-2 py-0.5 text-xs text-blue-400">{context}</span>;
}

function deviceStatusBadge(status: string) {
  const colors: Record<string, string> = {
    online: 'bg-emerald-500',
    offline: 'bg-red-500',
    unknown: 'bg-zinc-500',
  };
  const dot = colors[status] || 'bg-zinc-500';
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-zinc-400">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {status}
    </span>
  );
}

function serviceStatusBadge(status: string) {
  const styles: Record<string, string> = {
    active: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    degraded: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
    stopped: 'bg-red-500/10 border-red-500/20 text-red-400',
    unknown: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  };
  const cls = styles[status] || styles.unknown;
  return <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${cls}`}>{status}</span>;
}

function priorityColor(priority: string) {
  const colors: Record<string, string> = {
    critical: 'border-red-500/30 bg-red-500/5',
    high: 'border-orange-500/30 bg-orange-500/5',
    medium: 'border-amber-500/30 bg-amber-500/5',
    low: 'border-zinc-700 bg-zinc-900/50',
  };
  return colors[priority] || colors.low;
}

function priorityDot(priority: string) {
  const colors: Record<string, string> = {
    critical: 'bg-red-500',
    high: 'bg-orange-500',
    medium: 'bg-amber-500',
    low: 'bg-zinc-500',
  };
  return colors[priority] || 'bg-zinc-500';
}


// --- Page ---

export default function RegistryPage() {
  const [status, setStatus] = useState<RegistryStatus | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    async function load() {
      const [s, a] = await Promise.all([getRegistryStatus(), getAuditLog()]);
      if (!alive) return;
      setStatus(s);
      setAudit(Array.isArray(a) ? a : []);
      setLoading(false);
    }
    load();
    const id = setInterval(load, 15000);  // matches the old revalidate=15
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto space-y-8">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Registry</h2>
          <p className="text-zinc-500 mt-1">Central source of truth for devices, services, and ports</p>
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
          Loading…
        </div>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="max-w-6xl mx-auto space-y-8">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Registry</h2>
          <p className="text-zinc-500 mt-1">Central source of truth for devices, services, and ports</p>
        </div>
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center">
          <p className="text-red-400 font-medium">Registry Unreachable</p>
          <p className="text-zinc-500 text-sm mt-1">Could not connect to the registry API at localhost:7777</p>
        </div>
      </div>
    );
  }

  const { devices, services, ports, open_tickets: openTickets, counts } = status;

  // Group ports by type for the allocation map
  const portsByType: Record<string, Port[]> = {};
  for (const p of ports) {
    if (!portsByType[p.type]) portsByType[p.type] = [];
    portsByType[p.type].push(p);
  }

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Registry</h2>
          <p className="text-zinc-500 mt-1">Central source of truth for devices, services, and ports</p>
        </div>
        <a href="/" className="text-sm text-zinc-400 hover:text-white transition-colors shrink-0">
          &larr; Dashboard
        </a>
      </div>

      {/* System Status */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Devices" value={counts.devices} subValue={`${devices.filter(d => d.status === 'online').length} online`} />
        <StatCard label="Services" value={counts.services} subValue={`${services.filter(s => s.status === 'active').length} active`} />
        <StatCard label="Ports" value={counts.ports} subValue="allocated" />
        <StatCard label="Open Tickets" value={counts.open_tickets} subValue={openTickets.filter(t => t.priority === 'critical' || t.priority === 'high').length > 0 ? `${openTickets.filter(t => t.priority === 'critical' || t.priority === 'high').length} high/critical` : 'none urgent'} />
      </div>

      {/* Devices Table */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Devices</h3>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="px-4 py-3 font-medium">ID</th>
                  <th className="px-4 py-3 font-medium">Hostname</th>
                  <th className="px-4 py-3 font-medium">Context</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Tunnel Port</th>
                  <th className="px-4 py-3 font-medium hidden lg:table-cell">Startup</th>
                  <th className="px-4 py-3 font-medium hidden lg:table-cell">Recovery</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {devices.map((device) => (
                  <tr key={device.id} className="hover:bg-zinc-800/30 transition-colors">
                    <td className="px-4 py-3 font-mono text-zinc-200">{device.id}</td>
                    <td className="px-4 py-3 text-zinc-400">{device.hostname}</td>
                    <td className="px-4 py-3">{contextBadge(device.context)}</td>
                    <td className="px-4 py-3">{deviceStatusBadge(device.status)}</td>
                    <td className="px-4 py-3 font-mono text-zinc-400">{device.tunnel_port ?? <span className="text-zinc-600">--</span>}</td>
                    <td className="px-4 py-3 text-zinc-500 hidden lg:table-cell">{device.startup_method ?? <span className="text-zinc-700">--</span>}</td>
                    <td className="px-4 py-3 text-zinc-500 hidden lg:table-cell">{device.recovery_method ?? <span className="text-zinc-700">--</span>}</td>
                  </tr>
                ))}
                {devices.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-4 py-8 text-center text-zinc-600">No devices registered</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Services Table */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Services</h3>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">Device</th>
                  <th className="px-4 py-3 font-medium">Port</th>
                  <th className="px-4 py-3 font-medium">Host Port</th>
                  <th className="px-4 py-3 font-medium">Subdomain</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium hidden md:table-cell">Version</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {services.map((svc) => (
                  <tr key={svc.id} className="hover:bg-zinc-800/30 transition-colors">
                    <td className="px-4 py-3 font-mono text-zinc-200">{svc.name}</td>
                    <td className="px-4 py-3 text-zinc-400">{svc.device_id}</td>
                    <td className="px-4 py-3 font-mono text-zinc-400">{svc.port}</td>
                    <td className="px-4 py-3 font-mono text-zinc-400">{svc.slice_port ?? <span className="text-zinc-600">--</span>}</td>
                    <td className="px-4 py-3">
                      {svc.external_url ? (
                        <a
                          href={svc.external_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-400 hover:text-blue-300 transition-colors"
                        >
                          {svc.subdomain}.{svc.domain}
                        </a>
                      ) : (
                        <span className="text-zinc-600">--</span>
                      )}
                    </td>
                    <td className="px-4 py-3">{serviceStatusBadge(svc.status)}</td>
                    <td className="px-4 py-3 text-zinc-500 hidden md:table-cell">{svc.version ?? <span className="text-zinc-700">--</span>}</td>
                  </tr>
                ))}
                {services.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-4 py-8 text-center text-zinc-600">No services registered</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Port Allocation Map */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Port Allocation</h3>
        <div className="grid md:grid-cols-2 gap-4">
          {Object.entries(PORT_RANGES).map(([type, range]) => {
            const allocated = portsByType[type] || [];
            const total = range.hi - range.lo + 1;
            const used = allocated.length;
            const pct = total > 0 ? Math.round((used / total) * 100) : 0;

            return (
              <div key={type} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-medium text-zinc-200">{range.label}</h4>
                  <span className="text-xs text-zinc-500 font-mono">{range.lo}-{range.hi}</span>
                </div>
                <div className="flex items-center gap-3 mb-3">
                  <div className="flex-1 h-2 bg-zinc-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${pct > 80 ? 'bg-amber-500' : 'bg-emerald-500'}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-xs text-zinc-400 font-mono w-16 text-right">{used}/{total}</span>
                </div>
                {allocated.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {allocated.map((p) => (
                      <span
                        key={p.port}
                        className="inline-flex items-center rounded bg-zinc-800 px-1.5 py-0.5 text-xs font-mono text-zinc-400"
                        title={p.owner_id}
                      >
                        {p.port}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Open Tickets */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Open Tickets</h3>
        {openTickets.length > 0 ? (
          <div className="space-y-2">
            {openTickets.map((ticket) => (
              <div
                key={ticket.id}
                className={`rounded-lg border ${priorityColor(ticket.priority)} p-4`}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex items-start gap-3 min-w-0">
                    <span className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${priorityDot(ticket.priority)}`} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-mono text-zinc-500">#{ticket.id}</span>
                        <span className="text-sm text-zinc-200">{ticket.summary}</span>
                      </div>
                      <div className="flex items-center gap-3 mt-1 text-xs text-zinc-500">
                        <span>{ticket.priority}</span>
                        <span>{ticket.source}</span>
                        {ticket.device_id && <span>{ticket.device_id}</span>}
                        {ticket.service_id && <span>{ticket.service_id}</span>}
                      </div>
                    </div>
                  </div>
                  <span className="text-xs text-zinc-600 shrink-0 whitespace-nowrap">
                    {timeAgo(ticket.created_at)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No open tickets
          </div>
        )}
      </div>

      {/* Recent Audit Log */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Recent Audit Log</h3>
        {audit.length > 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 divide-y divide-zinc-800/50">
            {audit.map((entry) => (
              <div key={entry.id} className="flex items-center justify-between px-4 py-3 text-sm">
                <div className="flex items-center gap-3 min-w-0">
                  <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs font-mono text-zinc-400 shrink-0">
                    {entry.action}
                  </span>
                  {entry.target && (
                    <span className="text-zinc-300 truncate">{entry.target}</span>
                  )}
                </div>
                <span className="text-xs text-zinc-600 shrink-0 ml-4">
                  {entry.created_at ? timeAgo(entry.created_at) : ''}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No audit entries
          </div>
        )}
      </div>

      {/* Last Updated */}
      <div className="text-xs text-zinc-600 text-right">
        Last updated: {new Date().toLocaleTimeString()}
      </div>
    </div>
  );
}
