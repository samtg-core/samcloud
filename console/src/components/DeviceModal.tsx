'use client';

import { useState, useEffect, useCallback } from 'react';

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

interface GroupMembership {
  group_id: string;
  group_name: string;
}

interface AvailableGroup {
  id: string;
  name: string;
}

interface DeviceModalProps {
  device: Device | null;
  onClose: () => void;
  onUpdate: () => void;
}

interface Helper {
  name: string;
  description: string;
  filename: string;
  url: string;
}

interface ReEnrollResult {
  device_id: string;
  token: string;
  bootstrap_url: string;
  bootstrap_command?: string;
  bootstrap_command_ps1?: string;
  expires_in?: number;
}

const REGISTRY_PROXY = '/api/registry';

export default function DeviceModal({ device, onClose, onUpdate }: DeviceModalProps) {
  const [groups, setGroups] = useState<GroupMembership[]>([]);
  const [allGroups, setAllGroups] = useState<AvailableGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionMsg, setActionMsg] = useState('');
  const [addGroupId, setAddGroupId] = useState('');
  const [helpers, setHelpers] = useState<Helper[]>([]);
  const [reEnroll, setReEnroll] = useState<ReEnrollResult | null>(null);

  const loadDetails = useCallback(async () => {
    if (!device) return;
    setLoading(true);
    try {
      const groupsRes = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups`);
      if (groupsRes.ok) {
        const fetchedGroups: { id: string; name: string }[] = await groupsRes.json();
        setAllGroups(fetchedGroups.map(g => ({ id: g.id, name: g.name })));
        const memberships: GroupMembership[] = [];
        for (const g of fetchedGroups) {
          const detailRes = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${g.id}`);
          if (detailRes.ok) {
            const detail = await detailRes.json();
            const found = detail.devices?.some((d: { id: string }) => d.id === device.id);
            if (found) {
              memberships.push({ group_id: g.id, group_name: g.name });
            }
          }
        }
        setGroups(memberships);
      }

      const helpersRes = await fetch(`${REGISTRY_PROXY}?path=/api/v1/devices/${encodeURIComponent(device.id)}/helpers`);
      if (helpersRes.ok) {
        setHelpers(await helpersRes.json());
      }
    } catch {
      setActionMsg('Failed to load device details');
    } finally {
      setLoading(false);
    }
  }, [device]);

  async function handleReEnroll() {
    if (!device) return;
    setActionMsg('');
    setReEnroll(null);
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/enroll/${encodeURIComponent(device.id)}/retoken`, { method: 'POST' });
      if (res.ok) {
        setReEnroll(await res.json());
      } else {
        const data = await res.json().catch(() => ({}));
        setActionMsg(data.detail || `Re-enroll failed (${res.status})`);
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  useEffect(() => { loadDetails(); }, [loadDetails]);

  if (!device) return null;

  async function handleAddToGroup() {
    if (!addGroupId) return;
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${addGroupId}/devices`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: device!.id }),
      });
      if (res.ok) {
        setActionMsg(`Added to ${addGroupId}`);
        setAddGroupId('');
        loadDetails();
        onUpdate();
      } else {
        const data = await res.json();
        setActionMsg(data.detail || 'Failed to add to group');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  async function handleRemoveFromGroup(groupId: string) {
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/devices/${device!.id}`, {
        method: 'DELETE',
      });
      if (res.ok) {
        setActionMsg(`Removed from ${groupId}`);
        loadDetails();
        onUpdate();
      } else {
        setActionMsg('Failed to remove from group');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  const available = allGroups.filter(g => !groups.some(m => m.group_id === g.id));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-lg border border-zinc-800 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-zinc-800 px-6 py-4">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold font-mono text-zinc-100">{device.id}</h2>
            <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${
              device.status === 'online'
                ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
                : device.status === 'pending'
                  ? 'bg-amber-500/10 border-amber-500/20 text-amber-400'
                  : 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400'
            }`}>
              {device.status}
            </span>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 transition-colors text-lg leading-none">x</button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto px-6 py-4 space-y-6">
          {/* Info */}
          <div className="grid grid-cols-2 gap-3 text-xs">
            {device.name !== device.id && (
              <div><span className="text-zinc-500">Name: </span><span className="text-zinc-200">{device.name}</span></div>
            )}
            <div><span className="text-zinc-500">Type: </span><span className="text-zinc-200">{device.type}</span></div>
            <div><span className="text-zinc-500">Route: </span><span className="text-zinc-200">{device.inbound_method || 'none'}</span></div>
            <div><span className="text-zinc-500">Target: </span><span className="text-zinc-200">{device.outbound_method || 'none'}</span></div>
            {device.outbound_ip && (
              <div><span className="text-zinc-500">IP: </span><span className="font-mono text-zinc-200">{device.outbound_ip}</span></div>
            )}
            {device.outbound_ssh_user && (
              <div><span className="text-zinc-500">SSH User: </span><span className="font-mono text-zinc-200">{device.outbound_ssh_user}</span></div>
            )}
            {device.tunnel_port && (
              <div><span className="text-zinc-500">Tunnel: </span><span className="font-mono text-zinc-200">:{device.tunnel_port}</span></div>
            )}
            <div><span className="text-zinc-500">Created: </span><span className="text-zinc-200">{device.created_at}</span></div>
          </div>

          {device.ssh_user_warning && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-300">
              <strong>SSH user warning:</strong> {device.ssh_user_warning}
            </div>
          )}

          {actionMsg && (
            <div className="rounded-md border border-zinc-700 bg-zinc-800/50 p-2 text-xs text-zinc-300">
              {actionMsg}
            </div>
          )}

          {loading ? (
            <p className="text-sm text-zinc-500">Loading details...</p>
          ) : (
            <div className="space-y-3">
              <h3 className="text-sm font-medium text-zinc-300">Groups</h3>
              {groups.length > 0 ? (
                <div className="space-y-2">
                  {groups.map(g => (
                    <div key={g.group_id} className="flex items-center justify-between rounded-md border border-zinc-800 bg-zinc-800/30 px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-zinc-200">{g.group_name}</span>
                        <span className="text-xs font-mono text-zinc-500">{g.group_id}</span>
                      </div>
                      <button
                        onClick={() => handleRemoveFromGroup(g.group_id)}
                        className="text-xs text-red-400 hover:text-red-300 transition-colors"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-zinc-600">Not in any groups</p>
              )}
              {/* Add to group */}
              {available.length > 0 && (
                <div className="flex items-center gap-2 mt-2">
                  <select
                    value={addGroupId}
                    onChange={(e) => setAddGroupId(e.target.value)}
                    className="flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
                  >
                    <option value="">Add to group...</option>
                    {available.map(g => (
                      <option key={g.id} value={g.id}>{g.name} ({g.id})</option>
                    ))}
                  </select>
                  <button
                    onClick={handleAddToGroup}
                    disabled={!addGroupId}
                    className="rounded bg-blue-600 px-2 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors"
                  >
                    Add
                  </button>
                </div>
              )}

              {/* Helpers */}
              {helpers.length > 0 && (
                <div className="space-y-2 pt-3 border-t border-zinc-800/50">
                  <h3 className="text-sm font-medium text-zinc-300">Helpers</h3>
                  {helpers.map(h => {
                    const cmd = h.filename.endsWith('.ps1')
                      ? `irm "${h.url}" | iex`
                      : `curl -s "${h.url}" | bash`;
                    return (
                      <div key={h.name} className="rounded-md border border-zinc-800 bg-zinc-800/30 p-3 space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-mono text-zinc-200">{h.name}</span>
                          <span className="text-xs text-zinc-500">{h.filename}</span>
                        </div>
                        <p className="text-xs text-zinc-400">{h.description}</p>
                        <div className="flex items-center gap-2">
                          <code className="flex-1 text-xs font-mono text-zinc-300 break-all">{cmd}</code>
                          <button
                            onClick={() => navigator.clipboard.writeText(cmd)}
                            className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700"
                          >
                            Copy
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Re-enroll */}
              <div className="space-y-2 pt-3 border-t border-zinc-800/50">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium text-zinc-300">Re-enroll</h3>
                  <button
                    onClick={handleReEnroll}
                    className="rounded bg-amber-600/80 px-3 py-1 text-xs font-medium text-white hover:bg-amber-500 transition-colors"
                  >
                    Generate new token
                  </button>
                </div>
                {reEnroll && (
                  <div className="rounded-md border border-zinc-800 bg-zinc-800/30 p-3 space-y-2">
                    {reEnroll.bootstrap_command && (
                      <div>
                        <span className="text-xs text-zinc-500">macOS / Linux / WSL:</span>
                        <div className="flex items-center gap-2 mt-1">
                          <code className="flex-1 text-xs font-mono text-zinc-300 break-all">{reEnroll.bootstrap_command}</code>
                          <button onClick={() => reEnroll.bootstrap_command && navigator.clipboard.writeText(reEnroll.bootstrap_command)}
                            className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">Copy</button>
                        </div>
                      </div>
                    )}
                    {reEnroll.bootstrap_command_ps1 && (
                      <div>
                        <span className="text-xs text-zinc-500">Windows (PowerShell):</span>
                        <div className="flex items-center gap-2 mt-1">
                          <code className="flex-1 text-xs font-mono text-zinc-300 break-all">{reEnroll.bootstrap_command_ps1}</code>
                          <button onClick={() => reEnroll.bootstrap_command_ps1 && navigator.clipboard.writeText(reEnroll.bootstrap_command_ps1)}
                            className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">Copy</button>
                        </div>
                      </div>
                    )}
                    {reEnroll.expires_in && (
                      <p className="text-xs text-zinc-500">Token expires in {Math.round(reEnroll.expires_in / 60)} min</p>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
