'use client';

import { useState, useEffect, useCallback } from 'react';

interface User {
  id: number;
  username: string;
  role: string;
  scopes: string | null;
  created_at: string;
}

interface GroupMembership {
  group_id: string;
  group_name: string;
  role: string;
}

interface UserService {
  id: string;
  name: string;
  device_id: string;
  status: string;
}

interface AvailableGroup {
  id: string;
  name: string;
}

interface UserModalProps {
  user: User | null;
  onClose: () => void;
  onUpdate: () => void;
}

const REGISTRY_PROXY = '/api/registry';

export default function UserModal({ user, onClose, onUpdate }: UserModalProps) {
  const [groups, setGroups] = useState<GroupMembership[]>([]);
  const [allGroups, setAllGroups] = useState<AvailableGroup[]>([]);
  const [services, setServices] = useState<UserService[]>([]);
  const [loading, setLoading] = useState(true);
  const [resetMessage, setResetMessage] = useState('');
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [actionMsg, setActionMsg] = useState('');
  const [addGroupId, setAddGroupId] = useState('');
  const [addGroupRole, setAddGroupRole] = useState('viewer');

  const loadUserDetails = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    try {
      const [groupsRes, servicesRes] = await Promise.all([
        fetch(`${REGISTRY_PROXY}?path=/api/v1/groups`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/services`),
      ]);

      // Extract group memberships for this user from all groups
      if (groupsRes.ok) {
        const fetchedGroups: { id: string; name: string; members?: { id: number; group_role: string }[] }[] = await groupsRes.json();
        setAllGroups(fetchedGroups.map(g => ({ id: g.id, name: g.name })));
        const memberships: GroupMembership[] = [];
        // For each group, fetch details to check members
        for (const g of fetchedGroups) {
          const detailRes = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${g.id}`);
          if (detailRes.ok) {
            const detail = await detailRes.json();
            const member = detail.members?.find((m: { id: number }) => m.id === user.id);
            if (member) {
              memberships.push({
                group_id: g.id,
                group_name: g.name,
                role: member.group_role,
              });
            }
          }
        }
        setGroups(memberships);
      }

      // Filter services created by this user
      if (servicesRes.ok) {
        const allServices: (UserService & { created_by?: string })[] = await servicesRes.json();
        setServices(allServices.filter((s) => s.created_by === user.username));
      }
    } catch {
      setActionMsg('Failed to load user details');
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    loadUserDetails();
  }, [loadUserDetails]);

  if (!user) return null;

  async function handleRoleChange(groupId: string, newRole: string) {
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/members/${user!.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: newRole }),
      });
      if (res.ok) {
        setActionMsg(`Updated role in ${groupId} to ${newRole}`);
        loadUserDetails();
      } else {
        const data = await res.json();
        setActionMsg(data.detail || 'Failed to update role');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  async function handleRemoveFromGroup(groupId: string) {
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/members/${user!.id}`, {
        method: 'DELETE',
      });
      if (res.ok) {
        setActionMsg(`Removed from ${groupId}`);
        loadUserDetails();
        onUpdate();
      } else {
        setActionMsg('Failed to remove from group');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  async function handleAddToGroup() {
    if (!addGroupId) return;
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${addGroupId}/members`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: user!.id, role: addGroupRole }),
      });
      if (res.ok) {
        setActionMsg(`Added to ${addGroupId} as ${addGroupRole}`);
        setAddGroupId('');
        setAddGroupRole('viewer');
        loadUserDetails();
        onUpdate();
      } else {
        const data = await res.json();
        setActionMsg(data.detail || 'Failed to add to group');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  async function handleResetToken() {
    setResetMessage('');
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/users/${user!.id}/regenerate-token`, {
        method: 'POST',
      });
      const data = await res.json();
      if (res.ok && data.message) {
        setResetMessage(data.message);
        navigator.clipboard.writeText(data.message);
        setActionMsg('Token reset and copied to clipboard');
      } else {
        setActionMsg(data.detail || 'Failed to reset token');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  async function handleDeleteUser() {
    setActionMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/users/${user!.id}`, {
        method: 'DELETE',
      });
      if (res.ok) {
        onUpdate();
        onClose();
      } else {
        const data = await res.json();
        setActionMsg(data.detail || 'Failed to delete user');
      }
    } catch {
      setActionMsg('Error connecting to registry');
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-lg border border-zinc-800 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-zinc-800 px-6 py-4">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold text-zinc-100">{user.username}</h2>
            <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${
              user.role === 'admin'
                ? 'bg-purple-500/10 border-purple-500/20 text-purple-400'
                : user.role === 'agent'
                  ? 'bg-amber-500/10 border-amber-500/20 text-amber-400'
                  : 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400'
            }`}>
              {user.role}
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-300 transition-colors text-lg leading-none"
          >
            x
          </button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto px-6 py-4 space-y-6">
          {/* Info */}
          <div className="text-xs text-zinc-500">
            ID: {user.id} &middot; Created: {user.created_at}
            {user.scopes && (
              <div className="mt-1 font-mono">{user.scopes}</div>
            )}
          </div>

          {actionMsg && (
            <div className="rounded-md border border-zinc-700 bg-zinc-800/50 p-2 text-xs text-zinc-300">
              {actionMsg}
            </div>
          )}

          {loading ? (
            <p className="text-sm text-zinc-500">Loading details...</p>
          ) : (
            <>
              {/* Groups */}
              <div className="space-y-3">
                <h3 className="text-sm font-medium text-zinc-300">Groups</h3>
                {groups.length > 0 ? (
                  <div className="space-y-2">
                    {groups.map((g) => (
                      <div
                        key={g.group_id}
                        className="flex items-center justify-between rounded-md border border-zinc-800 bg-zinc-800/30 px-3 py-2"
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-sm text-zinc-200">{g.group_name}</span>
                          <span className="text-xs font-mono text-zinc-500">{g.group_id}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <select
                            value={g.role}
                            onChange={(e) => handleRoleChange(g.group_id, e.target.value)}
                            className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
                          >
                            <option value="viewer">viewer</option>
                            <option value="editor">editor</option>
                            <option value="owner">owner</option>
                          </select>
                          <button
                            onClick={() => handleRemoveFromGroup(g.group_id)}
                            className="text-xs text-red-400 hover:text-red-300 transition-colors"
                          >
                            Remove
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-zinc-600">Not a member of any groups</p>
                )}
                {/* Add to group */}
                {(() => {
                  const available = allGroups.filter(g => !groups.some(m => m.group_id === g.id));
                  if (available.length === 0) return null;
                  return (
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
                      <select
                        value={addGroupRole}
                        onChange={(e) => setAddGroupRole(e.target.value)}
                        className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
                      >
                        <option value="viewer">viewer</option>
                        <option value="editor">editor</option>
                        <option value="owner">owner</option>
                      </select>
                      <button
                        onClick={handleAddToGroup}
                        disabled={!addGroupId}
                        className="rounded bg-blue-600 px-2 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors"
                      >
                        Add
                      </button>
                    </div>
                  );
                })()}
              </div>

              {/* Services */}
              <div className="space-y-3">
                <h3 className="text-sm font-medium text-zinc-300">Services Created</h3>
                {services.length > 0 ? (
                  <div className="space-y-1">
                    {services.map((svc) => (
                      <div
                        key={svc.id}
                        className="flex items-center justify-between rounded-md border border-zinc-800 bg-zinc-800/30 px-3 py-2"
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-mono text-zinc-200">{svc.name || svc.id}</span>
                          <span className="text-xs text-zinc-500">{svc.device_id}</span>
                        </div>
                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${
                          svc.status === 'active'
                            ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
                            : 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400'
                        }`}>
                          {svc.status}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-zinc-600">No services created by this user</p>
                )}
              </div>
            </>
          )}

          {/* Token */}
          <div className="space-y-3">
            <h3 className="text-sm font-medium text-zinc-300">Token</h3>
            <button
              onClick={handleResetToken}
              className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 transition-colors"
            >
              Reset Token
            </button>
            {resetMessage && (
              <div className="rounded-md border border-blue-500/20 bg-blue-500/5 p-3 space-y-2">
                <pre className="text-xs text-zinc-200 whitespace-pre-wrap break-all font-mono bg-zinc-900 rounded p-2">
                  {resetMessage}
                </pre>
                <button
                  onClick={() => navigator.clipboard.writeText(resetMessage)}
                  className="rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700"
                >
                  Copy again
                </button>
              </div>
            )}
          </div>

          {/* Delete */}
          <div className="space-y-3 border-t border-zinc-800 pt-4">
            <h3 className="text-sm font-medium text-red-400">Danger Zone</h3>
            {!deleteConfirm ? (
              <button
                onClick={() => setDeleteConfirm(true)}
                className="rounded-md border border-red-500/20 px-3 py-1.5 text-xs text-red-400 hover:bg-red-500/10 transition-colors"
              >
                Delete User
              </button>
            ) : (
              <div className="flex items-center gap-3">
                <span className="text-xs text-zinc-400">Are you sure? This cannot be undone.</span>
                <button
                  onClick={handleDeleteUser}
                  className="rounded-md bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 transition-colors"
                >
                  Confirm Delete
                </button>
                <button
                  onClick={() => setDeleteConfirm(false)}
                  className="text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
