'use client';

import type { WatchdogIncident, WatchdogEscalation } from '@/lib/watchdog';

interface IncidentPanelProps {
  incidents: WatchdogIncident[];
  escalations: WatchdogEscalation[];
}

export function IncidentPanel({ incidents, escalations }: IncidentPanelProps) {
  const active = incidents.filter(i => i.status === 'active' || i.status === 'escalated');

  if (active.length === 0 && escalations.length === 0) {
    return null;
  }

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-semibold text-zinc-100">Active Incidents</h3>
      {active.map((inc) => (
        <div
          key={inc.id}
          className={`rounded-lg border p-4 ${
            inc.status === 'escalated'
              ? 'border-red-500/30 bg-red-500/5'
              : 'border-amber-500/20 bg-amber-500/5'
          }`}
        >
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${
                inc.status === 'escalated' ? 'bg-red-500' : 'bg-amber-500'
              }`} />
              <span className="font-medium text-zinc-100">
                {inc.device}/{inc.service}
              </span>
              {inc.status === 'escalated' && (
                <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-400">
                  escalated
                </span>
              )}
            </div>
            <span className="text-xs text-zinc-500">
              {formatRelativeTime(inc.opened_at)}
            </span>
          </div>
          {inc.reason && (
            <p className="text-sm text-zinc-400 mb-2">{inc.reason}</p>
          )}
          <div className="flex items-center gap-4 text-xs text-zinc-500">
            <span>Attempts: {inc.attempts}</span>
            {inc.timeline.length > 0 && (
              <span>Last: {inc.timeline[inc.timeline.length - 1].event}</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export function EscalationBanner({ escalations }: { escalations: WatchdogEscalation[] }) {
  const unacked = escalations.filter(e => !e.acknowledged);
  if (unacked.length === 0) return null;

  return (
    <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
      <div className="flex items-center gap-2 mb-2">
        <span className="h-2.5 w-2.5 rounded-full bg-red-500 animate-pulse" />
        <span className="font-semibold text-red-400">
          {unacked.length} escalation{unacked.length > 1 ? 's' : ''} need attention
        </span>
      </div>
      <div className="space-y-1">
        {unacked.map((esc) => (
          <div key={esc.id} className="text-sm text-zinc-300">
            <span className="font-medium">{esc.device}/{esc.service}</span>
            <span className="text-zinc-500"> — </span>
            <span className="text-zinc-400">{esc.reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
