'use client';

import { useState, useEffect, useCallback } from 'react';
import DeviceModal from '@/components/DeviceModal';

interface Device {
  id: string;
  name: string;
  type: string;
  context: string;
  owner_id: number | null;
  inbound_method: string | null;
  inbound_endpoint: string | null;
  outbound_method: string | null;
  outbound_ip: string | null;
  outbound_ssh_user: string | null;
  ssh_user_warning: string | null;
  tunnel_port: number | null;
  status: string;
  enrolled: number;
  created_at: string;
}

interface EnrollResult {
  device_id: string;
  name: string;
  status: string;
  // Access target fields (only present for targets)
  token?: string;
  bootstrap_url?: string;
  expires_in?: number;
  tunnel_port?: number;
  tunnel?: { host: string; port: number; user: string };
}

interface AccessKey {
  key_id: string;
  username: string;
  device: string;
  public_key: string;
  created_at: string;
}

interface Group {
  id: string;
  name: string;
}

type EnrollMode = 'route' | 'target' | 'both';

const PROXY = '/api/registry';

export default function DevicesPage() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [pending, setPending] = useState<Device[]>([]);
  const [accessKeys, setAccessKeys] = useState<AccessKey[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [deleteError, setDeleteError] = useState('');

  // Form state
  const [enrollMode, setEnrollMode] = useState<EnrollMode>('route');
  const [deviceId, setDeviceId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [deviceType, setDeviceType] = useState('');
  const [accessRoute, setAccessRoute] = useState('ssh-tunnel');
  const [accessTarget, setAccessTarget] = useState('');
  const [targetIp, setTargetIp] = useState('');
  const [targetSshUser, setTargetSshUser] = useState('');
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);
  const [enrolling, setEnrolling] = useState(false);
  const [enrollResult, setEnrollResult] = useState<EnrollResult | null>(null);
  const [enrollError, setEnrollError] = useState('');
  const [selectedDevice, setSelectedDevice] = useState<Device | null>(null);

  const loadData = useCallback(async () => {
    try {
      const [devRes, pendRes, keysRes, groupsRes] = await Promise.all([
        fetch(`${PROXY}?path=/api/v1/devices`),
        fetch(`${PROXY}?path=/api/v1/enroll/pending`),
        fetch(`${PROXY}?path=/api/v1/access-keys`),
        fetch(`${PROXY}?path=/api/v1/groups`),
      ]);
      if (devRes.ok) setDevices(await devRes.json());
      if (pendRes.ok) setPending(await pendRes.json());
      if (keysRes.ok) setAccessKeys(await keysRes.json());
      if (groupsRes.ok) setGroups(await groupsRes.json());
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  async function handleEnroll(e: React.FormEvent) {
    e.preventDefault();
    setEnrolling(true);
    setEnrollResult(null);
    setEnrollError('');

    try {
      const payload: Record<string, unknown> = {
        device_id: deviceId,
        name: displayName || deviceId,
        type: deviceType || 'device',
      };

      // Set access route/target based on mode
      if (enrollMode === 'route' || enrollMode === 'both') {
        payload.access_route = accessRoute;
      }
      if (enrollMode === 'target' || enrollMode === 'both') {
        payload.access_target = accessTarget;
        if (targetIp) payload.target_ip = targetIp;
        if (targetSshUser) payload.target_ssh_user = targetSshUser;
      }

      if (selectedGroups.length > 0) {
        payload.group_ids = selectedGroups;
      }

      const res = await fetch(`${PROXY}?path=/api/v1/enroll`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({ detail: `Registry returned ${res.status}` }));
      if (res.ok) {
        setEnrollResult(data);
        // Reset form
        setDeviceId('');
        setDisplayName('');
        setDeviceType('');
        setAccessRoute('ssh-tunnel');
        setAccessTarget('');
        setTargetIp('');
        setTargetSshUser('');
        setSelectedGroups([]);
        loadData();
      } else {
        setEnrollError(data.detail || `Enrollment failed (${res.status})`);
      }
    } catch (err) {
      setEnrollError(`Error connecting to registry: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setEnrolling(false);
    }
  }

  async function handleDelete(deviceId: string) {
    if (!confirm(`Delete device "${deviceId}"? This removes the device and all its services, tokens, and routes.`)) return;
    setDeleteError('');
    try {
      const res = await fetch(`${PROXY}?path=/api/v1/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
      if (res.ok) {
        loadData();
        if (enrollResult?.device_id === deviceId) setEnrollResult(null);
      } else {
        const data = await res.json().catch(() => ({ detail: `Delete failed (${res.status})` }));
        setDeleteError(`${deviceId}: ${data.detail || `Delete failed (${res.status})`}`);
      }
    } catch (err) {
      setDeleteError(`${deviceId}: ${err instanceof Error ? err.message : 'Could not reach registry'}`);
    }
  }

  async function handleReEnroll(deviceId: string) {
    const res = await fetch(`${PROXY}?path=/api/v1/enroll/${encodeURIComponent(deviceId)}/retoken`, { method: 'POST' });
    if (res.ok) {
      const data = await res.json();
      setEnrollResult(data);
    }
  }

  // Route-only devices go straight to pair
  const isRouteOnly = enrollMode === 'route';

  if (loading) return <div className="max-w-6xl mx-auto"><p className="text-zinc-500">Loading...</p></div>;
  if (error) return <div className="max-w-6xl mx-auto"><div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center"><p className="text-red-400">{error}</p></div></div>;

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <div>
        <div className="flex items-center gap-3 mb-1">
          <a href="/admin" className="text-sm text-zinc-500 hover:text-zinc-300">Admin</a>
          <span className="text-zinc-700">/</span>
          <h2 className="text-2xl font-bold text-zinc-100">Devices</h2>
        </div>
        <p className="text-zinc-500 mt-1">Enroll and manage devices</p>
      </div>

      {/* Enroll Form */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">Add Device</h3>

        {/* Mode selector */}
        <div className="flex gap-2">
          {([
            ['route', 'Access Route', 'Phone, laptop — accesses the network'],
            ['target', 'Access Target', 'Server, GPU — provides services'],
            ['both', 'Both', 'Mac, workstation — accesses and provides'],
          ] as const).map(([mode, label, desc]) => (
            <button key={mode} type="button" onClick={() => setEnrollMode(mode)}
              className={`flex-1 rounded-lg border p-3 text-left transition-colors ${
                enrollMode === mode
                  ? 'border-blue-500/50 bg-blue-500/10'
                  : 'border-zinc-800 bg-zinc-900/50 hover:bg-zinc-800/50'
              }`}>
              <span className={`text-sm font-medium ${enrollMode === mode ? 'text-blue-400' : 'text-zinc-300'}`}>{label}</span>
              <p className="text-xs text-zinc-500 mt-0.5">{desc}</p>
            </button>
          ))}
        </div>

        <form onSubmit={handleEnroll} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4">
          {/* Identity */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <input type="text" placeholder="Device ID *" value={deviceId} onChange={e => setDeviceId(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" required />
            <input type="text" placeholder="Display name" value={displayName} onChange={e => setDisplayName(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />
            <input type="text" placeholder="Type (phone, server, etc)" value={deviceType} onChange={e => setDeviceType(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />
          </div>

          {/* Access Route config */}
          {(enrollMode === 'route' || enrollMode === 'both') && (
            <div className="space-y-2">
              <label className="text-xs text-zinc-500 font-medium">Access Route — how this device reaches the network</label>
              <select value={accessRoute} onChange={e => setAccessRoute(e.target.value)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none">
                <option value="ssh-tunnel">SSH tunnel (via gateway)</option>
                <option value="lan">LAN (same network)</option>
                <option value="browser-only">Browser only (no SSH)</option>
              </select>
            </div>
          )}

          {/* Access Target config */}
          {(enrollMode === 'target' || enrollMode === 'both') && (
            <div className="space-y-3">
              <div className="space-y-2">
                <label className="text-xs text-zinc-500 font-medium">Access Target — how the gateway reaches this device</label>
                <select value={accessTarget} onChange={e => setAccessTarget(e.target.value)}
                  className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none">
                  <option value="">Select...</option>
                  <option value="direct-ssh">Direct SSH (LAN)</option>
                  <option value="reverse-tunnel">Reverse SSH tunnel (behind NAT)</option>
                  <option value="http">HTTP only</option>
                </select>
              </div>
              {accessTarget && accessTarget !== 'http' && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {accessTarget === 'direct-ssh' && (
                    <input type="text" placeholder="IP address" value={targetIp} onChange={e => setTargetIp(e.target.value)}
                      className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />
                  )}
                  <input type="text" placeholder="SSH user (auto-detected if blank)" value={targetSshUser} onChange={e => setTargetSshUser(e.target.value)}
                    className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />
                </div>
              )}
            </div>
          )}

          {/* Group assignment */}
          {groups.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-zinc-500 font-medium">Groups — add this device to</label>
              <div className="flex flex-wrap gap-2">
                {groups.map(g => (
                  <button key={g.id} type="button"
                    onClick={() => setSelectedGroups(prev =>
                      prev.includes(g.id) ? prev.filter(x => x !== g.id) : [...prev, g.id]
                    )}
                    className={`rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                      selectedGroups.includes(g.id)
                        ? 'border-blue-500/50 bg-blue-500/15 text-blue-400'
                        : 'border-zinc-700 bg-zinc-800 text-zinc-400 hover:bg-zinc-700'
                    }`}>
                    {g.name || g.id}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="flex items-center gap-3">
            <button type="submit" disabled={enrolling || (enrollMode !== 'route' && !accessTarget)}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors">
              {enrolling ? 'Adding...' : isRouteOnly ? 'Add & Generate Keys' : 'Add Device'}
            </button>
            {enrollError && <p className="text-xs text-red-400">{enrollError}</p>}
          </div>
        </form>

        {/* Enroll Result */}
        {enrollResult && (
          <EnrollResultCard result={enrollResult} isRouteOnly={!enrollResult.token} />
        )}
      </div>

      {/* Pending */}
      {pending.length > 0 && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold text-zinc-100">Pending Activation</h3>
          <p className="text-xs text-zinc-500">These devices need to run their bootstrap script to activate.</p>
          <div className="space-y-2">
            {pending.map(p => (
              <div key={p.id} className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-zinc-200 font-medium">{p.id}</span>
                    {p.name !== p.id && <span className="text-zinc-500 text-sm">{p.name}</span>}
                    <StatusBadge status="pending" />
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-zinc-600">{p.type}</span>
                    <button onClick={() => handleDelete(p.id)}
                      className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 hover:text-red-300 transition-colors">
                      Delete
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs mb-3">
                  <div><span className="text-zinc-600">Route: </span><span className="text-zinc-400">{p.inbound_method || 'none'}</span></div>
                  <div><span className="text-zinc-600">Target: </span><span className="text-zinc-400">{p.outbound_method || 'none'}</span></div>
                  {p.outbound_ip && <div><span className="text-zinc-600">IP: </span><span className="font-mono text-zinc-400">{p.outbound_ip}</span></div>}
                  {p.tunnel_port && <div><span className="text-zinc-600">Tunnel: </span><span className="font-mono text-zinc-400">:{p.tunnel_port}</span></div>}
                </div>
                <div className="flex gap-2">
                  <button onClick={() => handleReEnroll(p.id)}
                    className="rounded bg-amber-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-500 transition-colors">
                    Re-generate Token
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Delete error */}
      {deleteError && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-3 flex items-center justify-between">
          <p className="text-xs text-red-400">{deleteError}</p>
          <button onClick={() => setDeleteError('')} className="text-xs text-zinc-500 hover:text-zinc-300">dismiss</button>
        </div>
      )}

      {/* Active Devices */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">Devices ({devices.length})</h3>
        {devices.length > 0 ? (
          <div className="space-y-3">
            {devices.map(d => (
              <div key={d.id} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/30 transition-colors cursor-pointer"
                onClick={() => setSelectedDevice(d)}>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-zinc-200 font-medium">{d.id}</span>
                    {d.name !== d.id && <span className="text-zinc-500 text-sm">{d.name}</span>}
                    <StatusBadge status={d.status} />
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-zinc-600">{d.type}</span>
                    <button onClick={(e) => { e.stopPropagation(); handleDelete(d.id); }}
                      className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 hover:text-red-300 transition-colors">
                      Delete
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                  <div>
                    <span className="text-zinc-600">Route: </span>
                    <span className="text-zinc-400">{d.inbound_method || 'none'}</span>
                  </div>
                  <div>
                    <span className="text-zinc-600">Target: </span>
                    <span className="text-zinc-400">{d.outbound_method || 'none'}</span>
                  </div>
                  {d.outbound_ip && <div><span className="text-zinc-600">IP: </span><span className="font-mono text-zinc-400">{d.outbound_ip}</span></div>}
                  {d.tunnel_port && <div><span className="text-zinc-600">Tunnel: </span><span className="font-mono text-zinc-400">:{d.tunnel_port}</span></div>}
                </div>
                <div className="flex gap-2 mt-3 pt-3 border-t border-zinc-800/50" onClick={(e) => e.stopPropagation()}>
                  {d.inbound_method && (
                    <a href={`/pair?device=${encodeURIComponent(d.id)}`}
                      className="rounded bg-blue-600/80 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 transition-colors">
                      Pair / Access Key
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">No devices enrolled yet</div>
        )}
      </div>

      {/* Access Keys */}
      {accessKeys.length > 0 && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold text-zinc-100">Access Keys ({accessKeys.length})</h3>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                <th className="px-4 py-3">User</th><th className="px-4 py-3">Device</th><th className="px-4 py-3 hidden sm:table-cell">Public Key</th><th className="px-4 py-3 hidden md:table-cell">Created</th>
              </tr></thead>
              <tbody className="divide-y divide-zinc-800/50">
                {accessKeys.map(k => (
                  <tr key={k.key_id}><td className="px-4 py-3 text-zinc-200">{k.username}</td><td className="px-4 py-3 text-zinc-400">{k.device}</td><td className="px-4 py-3 font-mono text-zinc-500 text-xs truncate max-w-[200px] hidden sm:table-cell">{k.public_key}</td><td className="px-4 py-3 text-xs text-zinc-500 hidden md:table-cell">{k.created_at}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Device Detail Modal */}
      {selectedDevice && (
        <DeviceModal
          device={selectedDevice}
          onClose={() => setSelectedDevice(null)}
          onUpdate={loadData}
        />
      )}
    </div>
  );
}

function EnrollResultCard({ result, isRouteOnly }: { result: EnrollResult; isRouteOnly: boolean }) {
  if (isRouteOnly) {
    // Access route device — go straight to pair
    return (
      <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4 space-y-3">
        <h4 className="text-sm font-medium text-emerald-400">Device Enrolled — Ready for Pairing</h4>
        <p className="text-xs text-zinc-400">
          <strong className="text-zinc-200">{result.name}</strong> is active. Generate an access key to connect.
        </p>
        <a href={`/pair?device=${encodeURIComponent(result.device_id)}`}
          className="inline-flex items-center justify-center rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500">
          Generate Access Key
        </a>
      </div>
    );
  }

  // Access target — show bootstrap instructions
  // bootstrap_url may be absolute (registry returns full URL via PUBLIC_URL)
  // or relative (older registry); handle both.
  const fullBootstrapUrl = result.bootstrap_url
    ? (result.bootstrap_url.startsWith('http')
        ? result.bootstrap_url
        : (typeof window !== 'undefined' ? `${window.location.origin}${result.bootstrap_url}` : result.bootstrap_url))
    : null;
  const bashCmd = fullBootstrapUrl
    ? `curl -s "${fullBootstrapUrl}" | bash`
    : null;
  const ps1Cmd = fullBootstrapUrl
    ? `irm "${fullBootstrapUrl}/ps1" | iex`
    : null;

  return (
    <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4 space-y-3">
      <h4 className="text-sm font-medium text-emerald-400">Device Created — Awaiting Bootstrap</h4>
      <p className="text-xs text-zinc-400">
        <strong className="text-zinc-200">{result.name}</strong> is pending. Run the bootstrap on the device.
      </p>

      {/* Bootstrap commands */}
      {bashCmd && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/80 p-3 space-y-3">
          <div>
            <span className="text-xs text-zinc-500">macOS / Linux / WSL:</span>
            <div className="flex items-center gap-2 mt-1">
              <code className="flex-1 text-sm font-mono text-zinc-200 break-all">{bashCmd}</code>
              <button onClick={() => navigator.clipboard.writeText(bashCmd)}
                className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">
                Copy
              </button>
            </div>
          </div>
          <div>
            <span className="text-xs text-zinc-500">Windows (PowerShell):</span>
            <div className="flex items-center gap-2 mt-1">
              <code className="flex-1 text-sm font-mono text-zinc-200 break-all">{ps1Cmd}</code>
              <button onClick={() => ps1Cmd && navigator.clipboard.writeText(ps1Cmd)}
                className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">
                Copy
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="space-y-2 text-sm">
        <div>
          <span className="text-zinc-500">Token: </span>
          <code className="font-mono text-zinc-200 bg-zinc-800 px-2 py-0.5 rounded text-xs break-all cursor-pointer hover:bg-zinc-700"
            onClick={() => result.token && navigator.clipboard.writeText(result.token)} title="Click to copy">
            {result.token}
          </code>
        </div>
        {fullBootstrapUrl && (
          <div>
            <span className="text-zinc-500">Bootstrap URL: </span>
            <code className="font-mono text-zinc-400 bg-zinc-800 px-2 py-0.5 rounded text-xs break-all cursor-pointer hover:bg-zinc-700"
              onClick={() => fullBootstrapUrl && navigator.clipboard.writeText(fullBootstrapUrl)} title="Click to copy">
              {fullBootstrapUrl}
            </code>
          </div>
        )}
        {result.tunnel_port && (
          <div><span className="text-zinc-500">Tunnel Port: </span><code className="font-mono text-zinc-200">{result.tunnel_port}</code></div>
        )}
      </div>
      {result.tunnel_port && result.tunnel && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/80 p-3 space-y-2">
          <span className="text-xs text-zinc-500">Or manually set up the reverse tunnel:</span>
          <code className="block text-xs font-mono text-zinc-300 whitespace-pre-wrap">
{`autossh -M 0 -N \\
  -R ${result.tunnel_port}:localhost:22 \\
  -o ServerAliveInterval=30 \\
  -o ServerAliveCountMax=3 \\
  -i ~/.ssh/samcloud \\
  ${result.tunnel.user}@${result.tunnel.host} -p ${result.tunnel.port}`}
          </code>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    online: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    pending: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
    offline: 'bg-red-500/10 border-red-500/20 text-red-400',
    degraded: 'bg-orange-500/10 border-orange-500/20 text-orange-400',
    unknown: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${styles[status] || styles.unknown}`}>
      {status}
    </span>
  );
}
