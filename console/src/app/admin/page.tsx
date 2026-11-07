'use client';

import { useState, useEffect, useCallback } from 'react';
import UserModal from '@/components/UserModal';

interface User {
  id: number;
  username: string;
  role: string;
  scopes: string | null;
  created_at: string;
}

interface Group {
  id: string;
  name: string;
  description: string | null;
  route_path: string | null;
  created_at: string;
}

interface Device {
  id: string;
  name: string;
  status: string;
}

interface GroupSelection {
  group_id: string;
  role: string;
}

// All registry calls go through /api/registry proxy (handles env-based URL)
const REGISTRY_PROXY = '/api/registry';

export default function AdminPage() {
  const [users, setUsers] = useState<User[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // User modal
  const [selectedUser, setSelectedUser] = useState<User | null>(null);

  // User invite form
  const [newUsername, setNewUsername] = useState('');
  const [newRole, setNewRole] = useState('user');
  const [groupSelections, setGroupSelections] = useState<GroupSelection[]>([]);
  const [userMsg, setUserMsg] = useState('');
  const [inviteUrl, setInviteUrl] = useState('');
  const [inviteToken, setInviteToken] = useState('');
  const [briefUrl, setBriefUrl] = useState('');
  const [resetMessage, setResetMessage] = useState('');

  // Group form
  const [newGroupId, setNewGroupId] = useState('');
  const [newGroupName, setNewGroupName] = useState('');
  const [newGroupDesc, setNewGroupDesc] = useState('');
  const [newGroupRoute, setNewGroupRoute] = useState('');
  const [groupMsg, setGroupMsg] = useState('');

  const loadData = useCallback(async () => {
    try {
      const [usersRes, groupsRes, devicesRes] = await Promise.all([
        fetch(`${REGISTRY_PROXY}?path=/api/v1/users`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/groups`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/devices`),
      ]);
      if (usersRes.ok) setUsers(await usersRes.json());
      if (groupsRes.ok) setGroups(await groupsRes.json());
      if (devicesRes.ok) setDevices(await devicesRes.json());
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  function toggleGroup(groupId: string) {
    setGroupSelections((prev) => {
      const exists = prev.find((gs) => gs.group_id === groupId);
      if (exists) {
        return prev.filter((gs) => gs.group_id !== groupId);
      }
      return [...prev, { group_id: groupId, role: 'viewer' }];
    });
  }

  function updateGroupRole(groupId: string, role: string) {
    setGroupSelections((prev) =>
      prev.map((gs) => (gs.group_id === groupId ? { ...gs, role } : gs))
    );
  }

  async function handleInviteUser(e: React.FormEvent) {
    e.preventDefault();
    setUserMsg('');
    setInviteUrl('');
    try {
      // Build scopes from group selections, or admin wildcard
      const scopes = newRole === 'admin'
        ? ['*']
        : groupSelections.map((gs) => `group:${gs.group_id}`);

      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/users/invite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: newUsername,
          role: newRole,
          scopes,
          group_roles: newRole === 'admin' ? {} : groupSelections.reduce<Record<string, string>>((acc, gs) => {
            acc[gs.group_id] = gs.role;
            return acc;
          }, {}),
        }),
      });
      const data = await res.json();
      if (res.ok) {
        const consoleUrl = `${window.location.origin}/invite/${data.token}`;
        setInviteUrl(consoleUrl);
        setInviteToken(data.token);
        setBriefUrl(data.brief_url || '');
        setUserMsg(`Invited ${data.username} — share the message below`);
        setNewUsername('');
        setGroupSelections([]);
        setNewRole('user');
        loadData();
      } else {
        setUserMsg(data.detail || 'Failed to invite user');
      }
    } catch {
      setUserMsg('Error connecting to registry');
    }
  }

  async function handleCreateGroup(e: React.FormEvent) {
    e.preventDefault();
    setGroupMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: newGroupId,
          name: newGroupName,
          description: newGroupDesc || null,
          route_path: newGroupRoute || null,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setGroupMsg(`Created group "${data.group_id}"`);
        setNewGroupId('');
        setNewGroupName('');
        setNewGroupDesc('');
        setNewGroupRoute('');
        loadData();
      } else {
        setGroupMsg(data.detail || 'Failed to create group');
      }
    } catch {
      setGroupMsg('Error connecting to registry');
    }
  }

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto">
        <p className="text-zinc-500">Loading admin data...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-6xl mx-auto">
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center">
          <p className="text-red-400">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <div>
        <h2 className="text-2xl font-bold text-zinc-100">Admin</h2>
        <p className="text-zinc-500 mt-1">Manage users, groups, devices, and services</p>
      </div>

      {/* Quick Links */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <a
          href="/admin/devices"
          className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/50 transition-colors"
        >
          <h3 className="text-sm font-semibold text-zinc-100">Devices</h3>
          <p className="text-xs text-zinc-500 mt-1">Enroll and manage devices</p>
        </a>
        <a
          href="/admin/services"
          className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/50 transition-colors"
        >
          <h3 className="text-sm font-semibold text-zinc-100">Services</h3>
          <p className="text-xs text-zinc-500 mt-1">Register and manage services</p>
        </a>
        <a
          href="/admin/resources"
          className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/50 transition-colors"
        >
          <h3 className="text-sm font-semibold text-zinc-100">Resources</h3>
          <p className="text-xs text-zinc-500 mt-1">GPU, compute, leases</p>
        </a>
      </div>

      {/* Users Section */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">Users</h3>

        {users.length > 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="px-4 py-3 font-medium">ID</th>
                  <th className="px-4 py-3 font-medium">Username</th>
                  <th className="px-4 py-3 font-medium">Role</th>
                  <th className="px-4 py-3 font-medium">Created</th>
                  <th className="px-4 py-3 font-medium"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {users.map((user) => (
                  <tr
                    key={user.id}
                    className="hover:bg-zinc-800/30 transition-colors cursor-pointer"
                    onClick={() => setSelectedUser(user)}
                  >
                    <td className="px-4 py-3 font-mono text-zinc-400">{user.id}</td>
                    <td className="px-4 py-3 text-zinc-200">{user.username}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${
                        user.role === 'admin'
                          ? 'bg-purple-500/10 border-purple-500/20 text-purple-400'
                          : user.role === 'agent'
                            ? 'bg-amber-500/10 border-amber-500/20 text-amber-400'
                            : 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400'
                      }`}>
                        {user.role}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-zinc-500">{user.created_at}</td>
                    <td className="px-4 py-3 text-right">
                      <button onClick={async (e) => {
                        e.stopPropagation();
                        const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/users/${user.id}/regenerate-token`, { method: 'POST' });
                        const d = await res.json();
                        if (d.message) {
                          setResetMessage(d.message);
                          navigator.clipboard.writeText(d.message);
                        }
                      }} className="rounded px-2 py-1 text-xs text-blue-400 hover:bg-blue-500/10 transition-colors">
                        Reset Token
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No users yet. <a href="/setup" className="text-blue-400 hover:text-blue-300">Run setup</a> to create an admin.
          </div>
        )}

        {resetMessage && (
          <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-xs text-blue-400">Token reset — copied to clipboard. Paste this to the agent:</p>
              <button onClick={() => setResetMessage('')} className="text-xs text-zinc-500 hover:text-zinc-300">dismiss</button>
            </div>
            <pre className="text-xs text-zinc-200 whitespace-pre-wrap break-all font-mono bg-zinc-900 rounded p-3">{resetMessage}</pre>
            <button onClick={() => navigator.clipboard.writeText(resetMessage)}
              className="rounded bg-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">
              Copy again
            </button>
          </div>
        )}

        {/* Invite User Form */}
        <form onSubmit={handleInviteUser} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
          <h4 className="text-sm font-medium text-zinc-300">Invite User</h4>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <input
              type="text"
              placeholder="Username"
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
              required
            />
            <select
              value={newRole}
              onChange={(e) => setNewRole(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="user">user</option>
              <option value="admin">admin</option>
              <option value="agent">agent</option>
            </select>
          </div>
          {/* Group scope picker */}
          {newRole !== 'admin' && groups.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-zinc-500 block">Groups</label>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {groups.map((g) => {
                  const selection = groupSelections.find((gs) => gs.group_id === g.id);
                  const isChecked = !!selection;
                  return (
                    <div
                      key={g.id}
                      className={`flex items-center justify-between rounded-md border px-3 py-2 transition-colors ${
                        isChecked
                          ? 'border-blue-500/30 bg-blue-500/5'
                          : 'border-zinc-700 bg-zinc-800'
                      }`}
                    >
                      <label className="flex items-center gap-2 cursor-pointer flex-1">
                        <input
                          type="checkbox"
                          checked={isChecked}
                          onChange={() => toggleGroup(g.id)}
                          className="rounded border-zinc-600 bg-zinc-700 text-blue-600 focus:ring-blue-500 focus:ring-offset-0"
                        />
                        <span className="text-sm text-zinc-200">{g.name}</span>
                        <span className="text-xs font-mono text-zinc-500">{g.id}</span>
                      </label>
                      {isChecked && (
                        <select
                          value={selection!.role}
                          onChange={(e) => updateGroupRole(g.id, e.target.value)}
                          className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
                        >
                          <option value="viewer">viewer</option>
                          <option value="editor">editor</option>
                          <option value="owner">owner</option>
                        </select>
                      )}
                    </div>
                  );
                })}
              </div>
              {groupSelections.length > 0 && (
                <div className="text-xs text-zinc-500 font-mono">
                  Scopes: {groupSelections.map((gs) => `group:${gs.group_id}`).join(', ')}
                </div>
              )}
            </div>
          )}
          {newRole === 'admin' && (
            <p className="text-xs text-zinc-500">Admin users have full access to all groups and devices.</p>
          )}
          <button
            type="submit"
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 transition-colors"
          >
            Invite
          </button>
          {userMsg && <p className="text-xs text-zinc-400">{userMsg}</p>}
          {inviteUrl && (
            <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 space-y-3">
              <p className="text-xs text-zinc-400">Copy this message and send it to the user/agent:</p>
              {(() => {
                const apiBase = briefUrl ? briefUrl.replace(/\/api\/v1\/users\/brief\/.*/, '') : '';
                const activateUrl = `${apiBase}/api/v1/users/activate`;
                const msg = `You've been invited to samcloud.

1. Read your briefing:
   curl -s ${briefUrl}

2. Activate your account (choose a password):
   curl -s -X POST ${activateUrl} \\
     -H "Content-Type: application/json" \\
     -d '{"token":"${inviteToken}","password":"<choose-a-password>"}'

This returns your permanent API token and links to your guide, event stream, and API index.

Or open in browser: ${inviteUrl}`;
                return (
                  <>
                    <div className="rounded bg-zinc-900 p-3">
                      <pre className="text-sm text-zinc-200 whitespace-pre-wrap break-all font-mono">{msg}</pre>
                    </div>
                    <div className="flex gap-2">
                      <button type="button" onClick={() => navigator.clipboard.writeText(msg)}
                        className="rounded bg-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">
                        Copy message
                      </button>
                      <button type="button" onClick={() => navigator.clipboard.writeText(inviteUrl)}
                        className="rounded bg-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700">
                        Copy URL only
                      </button>
                    </div>
                  </>
                );
              })()}
            </div>
          )}
        </form>
      </div>

      {/* Groups Section */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">Groups</h3>

        {groups.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {groups.map((group) => (
              <a
                key={group.id}
                href={`/admin/groups/${group.id}`}
                className="block rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 hover:bg-zinc-800/50 transition-colors"
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="font-mono text-sm text-zinc-200">{group.id}</span>
                  {group.route_path && (
                    <span className="text-xs text-zinc-500 font-mono">{group.route_path}</span>
                  )}
                </div>
                <h4 className="text-sm font-medium text-zinc-100">{group.name}</h4>
                {group.description && (
                  <p className="text-xs text-zinc-500 mt-1">{group.description}</p>
                )}
              </a>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No groups yet
          </div>
        )}

        {/* Add Group Form */}
        <form onSubmit={handleCreateGroup} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
          <h4 className="text-sm font-medium text-zinc-300">Create Group</h4>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <input
              type="text"
              placeholder="Group ID (e.g. home)"
              value={newGroupId}
              onChange={(e) => setNewGroupId(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
              required
            />
            <input
              type="text"
              placeholder="Display Name"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
              required
            />
            <input
              type="text"
              placeholder="Description (optional)"
              value={newGroupDesc}
              onChange={(e) => setNewGroupDesc(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
            />
            <input
              type="text"
              placeholder="Route path (e.g. /home)"
              value={newGroupRoute}
              onChange={(e) => setNewGroupRoute(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
            />
          </div>
          <button
            type="submit"
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 transition-colors"
          >
            Create Group
          </button>
          {groupMsg && (
            <p className="text-xs text-zinc-400">{groupMsg}</p>
          )}
        </form>
      </div>

      {/* User Detail Modal */}
      {selectedUser && (
        <UserModal
          user={selectedUser}
          onClose={() => setSelectedUser(null)}
          onUpdate={loadData}
        />
      )}
    </div>
  );
}
