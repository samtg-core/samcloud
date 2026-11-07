'use client';

import { timeAgo } from '@/lib/time';
import { useEffect, useState } from 'react';
import { StatCard } from '@/components/StatusCard';
import type { Device, Service, GroupStatus } from '@/lib/registry';

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

export default function GroupDashboard({ params }: { params: Promise<{ groupId: string }> }) {
  const [groupId, setGroupId] = useState('');
  const [status, setStatus] = useState<GroupStatus | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => { params.then(p => setGroupId(p.groupId)); }, [params]);

  useEffect(() => {
    if (!groupId) return;
    let alive = true;
    // Scoped group view via the proxy (forwards the session token; registry
    // enforces group scope). /groups/{id}/status requires auth — no token = 401.
    async function load() {
      try {
        const res = await fetch(
          `/api/registry?path=${encodeURIComponent(`/api/v1/groups/${groupId}/status`)}`,
          { cache: 'no-store' },
        );
        if (alive) setStatus(res.ok ? await res.json() : null);
      } catch {
        if (alive) setStatus(null);
      } finally {
        if (alive) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 15000);  // matches old revalidate=15
    return () => { alive = false; clearInterval(id); };
  }, [groupId]);

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto space-y-8">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Group Dashboard</h2>
          <p className="text-zinc-500 mt-1">Loading…</p>
        </div>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="max-w-6xl mx-auto space-y-8">
        <div>
          <h2 className="text-2xl font-bold text-zinc-100">Group Dashboard</h2>
          <p className="text-zinc-500 mt-1">Group not found or registry offline</p>
        </div>
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center">
          <p className="text-red-400">Could not load group &quot;{groupId}&quot;</p>
          <p className="text-zinc-500 text-sm mt-1">
            Check that the group exists, you have access, and the registry API is running.
          </p>
        </div>
      </div>
    );
  }

  const { devices, services, counts, group } = status;
  const devicesByContext = groupByContext(devices);
  const servicesByDevice = groupByDevice(services);

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold text-zinc-100">{group.name}</h2>
        <p className="text-zinc-500 mt-1">{group.description || `Scoped view: ${groupId}`}</p>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Devices"
          value={counts.devices}
          subValue={`${counts.online_devices} online`}
        />
        <StatCard
          label="Services"
          value={counts.services}
          subValue={`${counts.active_services} active`}
        />
        <StatCard
          label="Routes"
          value={counts.routes}
        />
        <StatCard
          label="Group"
          value={groupId}
          subValue="scoped view"
        />
      </div>

      {/* Network Topology */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100 mb-4">Devices</h3>
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
              No devices in this group
            </div>
          )}
        </div>
      </div>

      {/* Services Table */}
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
            No services in this group
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="text-xs text-zinc-600 text-right">
        Last updated: {new Date().toLocaleTimeString()}
      </div>
    </div>
  );
}

// --- Sub-components (scoped copies to avoid import issues) ---

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
