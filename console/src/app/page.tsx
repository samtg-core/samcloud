'use client';

import { timeAgo } from '@/lib/time';
import { useEffect, useState } from 'react';
import { StatCard } from '@/components/StatusCard';
import type { Device, Service, Ticket, AuditEntry, RegistryStatus } from '@/lib/registry';


function groupByDevice(services: Service[]): Record<string, Service[]> {
  const groups: Record<string, Service[]> = {};
  for (const svc of services) (groups[svc.device_id] ||= []).push(svc);
  return groups;
}

function groupByContext(devices: Device[]): Record<string, Device[]> {
  const groups: Record<string, Device[]> = {};
  for (const dev of devices) (groups[dev.context] ||= []).push(dev);
  return groups;
}

// All registry reads go through the /api/registry proxy, which forwards the
// caller's session token; the registry filters by scope (fail closed). The
// dashboard never talks to the registry directly, so it cannot leak the mesh —
// a missing/unscoped token yields an empty fleet, not everything.
async function fetchRegistry<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`/api/registry?path=${encodeURIComponent(path)}`, { cache: 'no-store' });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export default function Dashboard() {
  const [status, setStatus] = useState<RegistryStatus | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    async function load() {
      const [s, a] = await Promise.all([
        fetchRegistry<RegistryStatus>('/api/v1/status'),
        fetchRegistry<AuditEntry[]>('/api/v1/audit?limit=10'),
      ]);
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
          <h2 className="text-2xl font-bold text-zinc-100">Dashboard</h2>
          <p className="text-zinc-500 mt-1">samcloud system overview</p>
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
          <h2 className="text-2xl font-bold text-zinc-100">Dashboard</h2>
          <p className="text-zinc-500 mt-1">samcloud system overview</p>
        </div>
        <RegistryOfflineBanner />
      </div>
    );
  }

  const { devices, services, open_tickets: openTickets, counts } = status;
  const activeServices = services.filter(s => s.status === 'active').length;
  const onlineDevices = devices.filter(d => d.status === 'online').length;
  const urgentTickets = openTickets.filter(t => t.priority === 'critical' || t.priority === 'high').length;
  const devicesByContext = groupByContext(devices);
  const servicesByDevice = groupByDevice(services);

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold text-zinc-100">Dashboard</h2>
        <p className="text-zinc-500 mt-1">samcloud system overview</p>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Devices"
          value={counts.devices}
          subValue={`${onlineDevices} online`}
        />
        <StatCard
          label="Services"
          value={counts.services}
          subValue={`${activeServices} active`}
        />
        <StatCard
          label="Routes"
          value={counts.routes}
          subValue={`${counts.ports} ports allocated`}
        />
        <StatCard
          label="Tickets"
          value={counts.open_tickets}
          subValue={urgentTickets > 0 ? `${urgentTickets} high/critical` : 'none urgent'}
        />
      </div>

      {/* Network Topology */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Network</h3>
        <div className="grid md:grid-cols-2 gap-4">
          {Object.entries(devicesByContext).map(([context, contextDevices]) => (
            <div key={context} className="space-y-3">
              <div className="flex items-center gap-2">
                <ContextBadge context={context} />
                <span className="text-xs text-zinc-500">
                  {contextDevices.length} device{contextDevices.length !== 1 ? 's' : ''}
                </span>
              </div>
              {contextDevices.map((device) => (
                <DeviceCard
                  key={device.id}
                  device={device}
                  services={servicesByDevice[device.id] || []}
                />
              ))}
            </div>
          ))}
          {Object.keys(devicesByContext).length === 0 && (
            <div className="col-span-2 rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
              No devices registered
            </div>
          )}
        </div>
      </div>

      {/* Services Grid */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Services</h3>
        {services.length > 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                    <th className="px-4 py-3 font-medium">Service</th>
                    <th className="px-4 py-3 font-medium">Device</th>
                    <th className="px-4 py-3 font-medium">Port</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium hidden md:table-cell">External</th>
                    <th className="px-4 py-3 font-medium hidden lg:table-cell">Last Check</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {services.map((svc) => (
                    <ServiceRow key={svc.id} service={svc} />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No services registered
          </div>
        )}
      </div>

      {/* Tickets */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-zinc-100">Open Tickets</h3>
          <a href="/tickets" className="text-sm text-zinc-400 hover:text-white transition-colors">
            View all
          </a>
        </div>
        <TicketList tickets={openTickets} />
      </div>

      {/* Recent Activity */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-zinc-100">Recent Activity</h3>
          <a href="/registry" className="text-sm text-zinc-400 hover:text-white transition-colors">
            Full registry
          </a>
        </div>
        <AuditList entries={audit} />
      </div>

      {/* Last Updated */}
      <div className="text-xs text-zinc-600 text-right">
        Last updated: {new Date().toLocaleTimeString()}
      </div>
    </div>
  );
}

// --- Sub-components ---

function RegistryOfflineBanner() {
  return (
    <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center">
      <p className="text-red-400 font-medium">Registry Offline</p>
      <p className="text-zinc-500 text-sm mt-1">
        Could not connect to the registry API at localhost:7777.
        The dashboard requires the registry to display system status.
      </p>
    </div>
  );
}

function ContextBadge({ context }: { context: string }) {
  if (context === 'tunnel') {
    return (
      <span className="inline-flex items-center rounded-full bg-purple-500/10 border border-purple-500/20 px-2.5 py-0.5 text-xs font-medium text-purple-400">
        {context}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-blue-500/10 border border-blue-500/20 px-2.5 py-0.5 text-xs font-medium text-blue-400">
      {context}
    </span>
  );
}

function DeviceStatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    online: 'bg-emerald-500',
    degraded: 'bg-amber-500',
    offline: 'bg-red-500',
    pending: 'bg-blue-500',
    unknown: 'bg-zinc-500',
  };
  return <span className={`h-2 w-2 rounded-full shrink-0 ${colors[status] || 'bg-zinc-500'}`} />;
}

function DeviceCard({ device, services }: { device: Device; services: Service[] }) {
  const activeCount = services.filter(s => s.status === 'active').length;

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <DeviceStatusDot status={device.status} />
          <span className="font-mono text-sm text-zinc-200">{device.id}</span>
        </div>
        <span className="text-xs text-zinc-500">{device.status}</span>
      </div>
      <div className="flex items-center gap-4 text-xs text-zinc-500 mb-3">
        <span>{device.hostname}</span>
        {device.ip && <span className="font-mono">{device.ip}</span>}
        {device.tunnel_port && (
          <span className="font-mono">tunnel:{device.tunnel_port}</span>
        )}
      </div>
      {services.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {services.map((svc) => (
            <ServicePill key={svc.id} service={svc} />
          ))}
          <span className="text-xs text-zinc-600 self-center ml-1">
            {activeCount}/{services.length} active
          </span>
        </div>
      )}
      {services.length === 0 && (
        <span className="text-xs text-zinc-600">No services</span>
      )}
    </div>
  );
}

function ServicePill({ service }: { service: Service }) {
  const statusColors: Record<string, string> = {
    active: 'bg-emerald-500',
    degraded: 'bg-amber-500',
    stopped: 'bg-red-500',
    unknown: 'bg-zinc-500',
  };
  const dot = statusColors[service.status] || 'bg-zinc-500';

  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-800/50 px-2 py-0.5 text-xs">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      <span className="text-zinc-400">{service.name}</span>
    </span>
  );
}

function ServiceStatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    degraded: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
    stopped: 'bg-red-500/10 border-red-500/20 text-red-400',
    unknown: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  };
  const cls = styles[status] || styles.unknown;
  return <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${cls}`}>{status}</span>;
}

function ServiceRow({ service }: { service: Service }) {
  return (
    <tr className="hover:bg-zinc-800/30 transition-colors">
      <td className="px-4 py-3 font-mono text-zinc-200">{service.name}</td>
      <td className="px-4 py-3 text-zinc-400">{service.device_id}</td>
      <td className="px-4 py-3 font-mono text-zinc-400">
        {service.port}
        {service.slice_port && (
          <span className="text-zinc-600"> / {service.slice_port}</span>
        )}
      </td>
      <td className="px-4 py-3">
        <ServiceStatusBadge status={service.status} />
      </td>
      <td className="px-4 py-3 hidden md:table-cell">
        {service.external_url ? (
          <a
            href={service.external_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 hover:text-blue-300 transition-colors text-xs"
          >
            {service.subdomain}.{service.domain}
          </a>
        ) : (
          <span className="text-zinc-600">--</span>
        )}
      </td>
      <td className="px-4 py-3 hidden lg:table-cell text-xs text-zinc-500">
        {service.last_health ? timeAgo(service.last_health) : '--'}
      </td>
    </tr>
  );
}

function TicketList({ tickets }: { tickets: Ticket[] }) {
  if (tickets.length === 0) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
        No open tickets
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {tickets.map((ticket) => (
        <TicketRow key={ticket.id} ticket={ticket} />
      ))}
    </div>
  );
}

function TicketRow({ ticket }: { ticket: Ticket }) {
  const priorityColors: Record<string, string> = {
    critical: 'border-red-500/30 bg-red-500/5',
    high: 'border-orange-500/30 bg-orange-500/5',
    medium: 'border-amber-500/30 bg-amber-500/5',
    low: 'border-zinc-700 bg-zinc-900/50',
  };
  const priorityDots: Record<string, string> = {
    critical: 'bg-red-500',
    high: 'bg-orange-500',
    medium: 'bg-amber-500',
    low: 'bg-zinc-500',
  };

  return (
    <a
      href={`/tickets?highlight=${ticket.id}`}
      className={`block rounded-lg border ${priorityColors[ticket.priority] || priorityColors.low} p-4 transition-colors hover:brightness-110`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 min-w-0">
          <span className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${priorityDots[ticket.priority] || 'bg-zinc-500'}`} />
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
    </a>
  );
}

function AuditList({ entries }: { entries: AuditEntry[] }) {
  if (entries.length === 0) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
        No recent activity
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 divide-y divide-zinc-800/50">
      {entries.map((entry) => (
        <div key={entry.id} className="flex items-center justify-between px-4 py-3 text-sm">
          <div className="flex items-center gap-3 min-w-0">
            <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs font-mono text-zinc-400 shrink-0">
              {entry.action}
            </span>
            {entry.target && (
              <span className="text-zinc-300 truncate">{entry.target}</span>
            )}
            {entry.actor && entry.actor !== 'system:registry' && (
              <span className="text-zinc-600 text-xs truncate">{entry.actor}</span>
            )}
          </div>
          <span className="text-xs text-zinc-600 shrink-0 ml-4">
            {entry.created_at ? timeAgo(entry.created_at) : ''}
          </span>
        </div>
      ))}
    </div>
  );
}
