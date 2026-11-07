'use client';

import { HealthStatus } from '@/lib/types';

interface StatusCardProps {
  name: string;
  status: HealthStatus;
  icon?: string;
  url?: string;
  stats?: { label: string; value: string | number }[];
  issues?: string[];
}

const statusColors: Record<HealthStatus, string> = {
  healthy: 'bg-emerald-500',
  degraded: 'bg-amber-500',
  unhealthy: 'bg-red-500',
  unknown: 'bg-zinc-500',
};

const statusBorders: Record<HealthStatus, string> = {
  healthy: 'border-emerald-500/20',
  degraded: 'border-amber-500/20',
  unhealthy: 'border-red-500/20',
  unknown: 'border-zinc-500/20',
};

export function StatusCard({ name, status, url, stats, issues }: StatusCardProps) {
  const content = (
    <div className={`rounded-lg border ${statusBorders[status]} bg-zinc-900/50 p-4 transition-all hover:bg-zinc-900`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-medium text-zinc-100">{name}</h3>
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${statusColors[status]}`} />
          <span className="text-xs text-zinc-500 capitalize">{status}</span>
        </div>
      </div>

      {stats && stats.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {stats.map((stat) => (
            <div key={stat.label} className="text-sm">
              <span className="text-zinc-500">{stat.label}: </span>
              <span className="text-zinc-300 font-mono">{stat.value}</span>
            </div>
          ))}
        </div>
      )}

      {issues && issues.length > 0 && (
        <div className="mt-2 text-xs text-amber-400/80 truncate">
          {issues[0]}
        </div>
      )}
    </div>
  );

  if (url) {
    return (
      <a href={url} target="_blank" rel="noopener noreferrer" className="block">
        {content}
      </a>
    );
  }

  return content;
}

// Summary stat card
interface StatCardProps {
  label: string;
  value: string | number;
  subValue?: string;
  trend?: 'up' | 'down' | 'neutral';
}

export function StatCard({ label, value, subValue }: StatCardProps) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="text-sm text-zinc-500 mb-1">{label}</div>
      <div className="text-2xl font-semibold text-zinc-100 font-mono">{value}</div>
      {subValue && <div className="text-xs text-zinc-500 mt-1">{subValue}</div>}
    </div>
  );
}

// Bundle card (for media/smarthome bundles)
interface BundleCardProps {
  name: string;
  description: string;
  status: HealthStatus;
  services: { name: string; status: HealthStatus }[];
  href: string;
}

export function BundleCard({ name, description, status, services, href }: BundleCardProps) {
  return (
    <a
      href={href}
      className={`block rounded-xl border ${statusBorders[status]} bg-zinc-900/30 p-6 transition-all hover:bg-zinc-900/50`}
    >
      <div className="flex items-start justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold text-zinc-100">{name}</h2>
          <p className="text-sm text-zinc-500">{description}</p>
        </div>
        <div className={`h-3 w-3 rounded-full ${statusColors[status]}`} />
      </div>

      <div className="flex flex-wrap gap-2">
        {services.map((service) => (
          <div
            key={service.name}
            className="flex items-center gap-1.5 rounded-full bg-zinc-800/50 px-2.5 py-1 text-xs"
          >
            <span className={`h-1.5 w-1.5 rounded-full ${statusColors[service.status]}`} />
            <span className="text-zinc-400">{service.name}</span>
          </div>
        ))}
      </div>
    </a>
  );
}
