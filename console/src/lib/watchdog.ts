import { readFile } from 'fs/promises';
import { homedir } from 'os';
import { join } from 'path';

const HEALTH_DIR = join(process.env.HOME || homedir(), '.local/samcloud/health');

// Types

export type WatchdogStatus = 'healthy' | 'unhealthy' | 'unknown';

export interface WatchdogServiceState {
  device: string;
  service: string;
  status: WatchdogStatus;
  reason: string | null;
  checked_at: string;
  previous_status: string | null;
}

export interface WatchdogHealth {
  services: Record<string, WatchdogServiceState>;
  last_run: string | null;
}

export interface WatchdogIncident {
  id: string;
  device: string;
  service: string;
  status: 'active' | 'escalated' | 'resolved';
  reason: string | null;
  opened_at: string;
  resolved_at?: string;
  attempts: number;
  last_attempt: string | null;
  timeline: { event: string; at: string; reason?: string; outcome?: string }[];
}

export interface WatchdogEscalation {
  id: string;
  device: string;
  service: string;
  reason: string;
  created_at: string;
  acknowledged: boolean;
}

// Readers

async function readJSON<T>(filename: string, fallback: T): Promise<T> {
  try {
    const data = await readFile(join(HEALTH_DIR, filename), 'utf-8');
    return JSON.parse(data) as T;
  } catch {
    return fallback;
  }
}

export async function getWatchdogHealth(): Promise<WatchdogHealth> {
  return readJSON<WatchdogHealth>('health.json', { services: {}, last_run: null });
}

export async function getWatchdogIncidents(): Promise<WatchdogIncident[]> {
  return readJSON<WatchdogIncident[]>('incidents.json', []);
}

export async function getWatchdogEscalations(): Promise<WatchdogEscalation[]> {
  return readJSON<WatchdogEscalation[]>('escalations.json', []);
}

// Derived helpers

export function isWatchdogStale(health: WatchdogHealth): boolean {
  if (!health.last_run) return true;
  const lastRun = new Date(health.last_run).getTime();
  const tenMinutesAgo = Date.now() - 10 * 60 * 1000;
  return lastRun < tenMinutesAgo;
}

export function getActiveIncidents(incidents: WatchdogIncident[]): WatchdogIncident[] {
  return incidents.filter(i => i.status === 'active' || i.status === 'escalated');
}

export function getUnacknowledgedEscalations(escalations: WatchdogEscalation[]): WatchdogEscalation[] {
  return escalations.filter(e => !e.acknowledged);
}

export function getOverallStatus(health: WatchdogHealth): WatchdogStatus {
  const statuses = Object.values(health.services).map(s => s.status);
  if (statuses.length === 0) return 'unknown';
  if (statuses.includes('unhealthy')) return 'unhealthy';
  return 'healthy';
}
