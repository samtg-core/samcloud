'use client';

import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'next/navigation';

interface Service {
  id: string;
  name: string;
  device_id: string;
  port: number;
  status: string;
  external_url: string | null;
}

interface Member {
  id: number;
  username: string;
  user_role: string;
  group_role: string;
  added_at: string;
}

interface GroupDevice {
  id: string;
  name: string;
  status: string;
  ip: string | null;
}

interface GroupDetail {
  id: string;
  name: string;
  description: string | null;
  route_path: string | null;
  services: Service[];
  members: Member[];
}

interface AvailableService {
  id: string;
  name: string;
  device_id: string;
  status: string;
}

interface AvailableUser {
  id: number;
  username: string;
  role: string;
}

interface AvailableDevice {
  id: string;
  name: string;
  status: string;
}

const REGISTRY_PROXY = '/api/registry';

export default function GroupDetailPage() {
  const params = useParams();
  const groupId = params.id as string;

  const [group, setGroup] = useState<GroupDetail | null>(null);
  const [groupDevices, setGroupDevices] = useState<GroupDevice[]>([]);
  const [allServices, setAllServices] = useState<AvailableService[]>([]);
  const [allUsers, setAllUsers] = useState<AvailableUser[]>([]);
  const [allDevices, setAllDevices] = useState<AvailableDevice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [msg, setMsg] = useState('');

  // Add service form
  const [selectedService, setSelectedService] = useState('');

  // Add member form
  const [selectedUser, setSelectedUser] = useState('');
  const [selectedRole, setSelectedRole] = useState('viewer');

  // Add device form
  const [selectedDevice, setSelectedDevice] = useState('');

  const loadGroup = useCallback(async () => {
    try {
      const [groupRes, devicesInGroupRes, servicesRes, usersRes, allDevicesRes] = await Promise.all([
        fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/devices`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/services`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/users`),
        fetch(`${REGISTRY_PROXY}?path=/api/v1/devices`),
      ]);
      if (groupRes.ok) {
        setGroup(await groupRes.json());
      } else {
        setError('Group not found');
      }
      if (devicesInGroupRes.ok) setGroupDevices(await devicesInGroupRes.json());
      if (servicesRes.ok) setAllServices(await servicesRes.json());
      if (usersRes.ok) setAllUsers(await usersRes.json());
      if (allDevicesRes.ok) setAllDevices(await allDevicesRes.json());
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLoading(false);
    }
  }, [groupId]);

  useEffect(() => {
    loadGroup();
  }, [loadGroup]);

  async function handleAddService(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedService) return;
    setMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/services`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service_id: selectedService }),
      });
      const data = await res.json();
      if (res.ok) {
        setMsg(`Added service ${selectedService}`);
        setSelectedService('');
        loadGroup();
      } else {
        setMsg(data.detail || 'Failed to add service');
      }
    } catch {
      setMsg('Error connecting to registry');
    }
  }

  async function handleRemoveService(serviceId: string) {
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/services/${serviceId}`, {
        method: 'DELETE',
      });
      if (res.ok) {
        setMsg(`Removed service ${serviceId}`);
        loadGroup();
      }
    } catch {
      setMsg('Error removing service');
    }
  }

  async function handleAddMember(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedUser) return;
    setMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/members`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: parseInt(selectedUser), role: selectedRole }),
      });
      const data = await res.json();
      if (res.ok) {
        setMsg(`Added member (user ${selectedUser})`);
        setSelectedUser('');
        setSelectedRole('viewer');
        loadGroup();
      } else {
        setMsg(data.detail || 'Failed to add member');
      }
    } catch {
      setMsg('Error connecting to registry');
    }
  }

  async function handleRemoveMember(userId: number) {
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/members/${userId}`, {
        method: 'DELETE',
      });
      if (res.ok) {
        setMsg(`Removed member (user ${userId})`);
        loadGroup();
      }
    } catch {
      setMsg('Error removing member');
    }
  }

  async function handleAddDevice(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedDevice) return;
    setMsg('');
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/devices`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: selectedDevice }),
      });
      const data = await res.json();
      if (res.ok) {
        setMsg(`Added device ${selectedDevice}`);
        setSelectedDevice('');
        loadGroup();
      } else {
        setMsg(data.detail || 'Failed to add device');
      }
    } catch {
      setMsg('Error connecting to registry');
    }
  }

  async function handleRemoveDevice(deviceId: string) {
    try {
      const res = await fetch(`${REGISTRY_PROXY}?path=/api/v1/groups/${groupId}/devices/${deviceId}`, {
        method: 'DELETE',
      });
      if (res.ok) {
        setMsg(`Removed device ${deviceId}`);
        loadGroup();
      }
    } catch {
      setMsg('Error removing device');
    }
  }

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto">
        <p className="text-zinc-500">Loading group...</p>
      </div>
    );
  }

  if (error || !group) {
    return (
      <div className="max-w-6xl mx-auto">
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center">
          <p className="text-red-400">{error || 'Group not found'}</p>
        </div>
      </div>
    );
  }

  const groupServiceIds = new Set(group.services.map((s) => s.id));
  const groupMemberIds = new Set(group.members.map((m) => m.id));
  const groupDeviceIds = new Set(groupDevices.map((d) => d.id));
  const availableServices = allServices.filter((s) => !groupServiceIds.has(s.id));
  const availableUsers = allUsers.filter((u) => !groupMemberIds.has(u.id));
  const availableDevices = allDevices.filter((d) => !groupDeviceIds.has(d.id));

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      {/* Header */}
      <div className="flex items-center gap-4">
        <a href="/admin" className="text-zinc-500 hover:text-zinc-300 text-sm transition-colors">
          Admin
        </a>
        <span className="text-zinc-600">/</span>
        <span className="text-zinc-300">{group.name}</span>
      </div>

      {/* Group Info */}
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-xl font-bold text-zinc-100">{group.name}</h2>
            <p className="text-sm font-mono text-zinc-500 mt-1">{group.id}</p>
            {group.description && (
              <p className="text-sm text-zinc-400 mt-2">{group.description}</p>
            )}
          </div>
          <div className="flex gap-3">
            {group.route_path && (
              <span className="inline-flex items-center rounded-full bg-blue-500/10 border border-blue-500/20 px-2.5 py-0.5 text-xs text-blue-400">
                {group.route_path}
              </span>
            )}
            <a
              href={`/g/${group.id}`}
              className="inline-flex items-center rounded-md bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-700 transition-colors"
            >
              Preview Dashboard
            </a>
          </div>
        </div>
      </div>

      {msg && (
        <div className="rounded-md border border-zinc-700 bg-zinc-800/50 p-3 text-sm text-zinc-300">
          {msg}
        </div>
      )}

      {/* Services */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">
          Services <span className="text-zinc-500 text-sm font-normal">({group.services.length})</span>
        </h3>

        {group.services.length > 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="px-4 py-3 font-medium">Service</th>
                  <th className="px-4 py-3 font-medium">Device</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">External</th>
                  <th className="px-4 py-3 font-medium w-20"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {group.services.map((svc) => (
                  <tr key={svc.id} className="hover:bg-zinc-800/30 transition-colors">
                    <td className="px-4 py-3 font-mono text-zinc-200">{svc.name}</td>
                    <td className="px-4 py-3 text-zinc-400">{svc.device_id}</td>
                    <td className="px-4 py-3">
                      <StatusBadge status={svc.status} />
                    </td>
                    <td className="px-4 py-3 text-xs text-zinc-500">
                      {svc.external_url || '--'}
                    </td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleRemoveService(svc.id)}
                        className="text-xs text-red-400 hover:text-red-300 transition-colors"
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No services in this group
          </div>
        )}

        {/* Add Service Form */}
        {availableServices.length > 0 && (
          <form onSubmit={handleAddService} className="flex items-center gap-3">
            <select
              value={selectedService}
              onChange={(e) => setSelectedService(e.target.value)}
              className="flex-1 rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="">Select a service to add...</option>
              {availableServices.map((svc) => (
                <option key={svc.id} value={svc.id}>
                  {svc.id} ({svc.status})
                </option>
              ))}
            </select>
            <button
              type="submit"
              disabled={!selectedService}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Add Service
            </button>
          </form>
        )}
      </div>

      {/* Devices */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">
          Devices <span className="text-zinc-500 text-sm font-normal">({groupDevices.length})</span>
        </h3>

        {groupDevices.length > 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="px-4 py-3 font-medium">Device</th>
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">IP</th>
                  <th className="px-4 py-3 font-medium w-20"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {groupDevices.map((device) => (
                  <tr key={device.id} className="hover:bg-zinc-800/30 transition-colors">
                    <td className="px-4 py-3 font-mono text-zinc-200">{device.id}</td>
                    <td className="px-4 py-3 text-zinc-400">{device.name}</td>
                    <td className="px-4 py-3">
                      <StatusBadge status={device.status} />
                    </td>
                    <td className="px-4 py-3 text-xs text-zinc-500">
                      {device.ip || '--'}
                    </td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleRemoveDevice(device.id)}
                        className="text-xs text-red-400 hover:text-red-300 transition-colors"
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No devices in this group
          </div>
        )}

        {/* Add Device Form */}
        {availableDevices.length > 0 && (
          <form onSubmit={handleAddDevice} className="flex items-center gap-3">
            <select
              value={selectedDevice}
              onChange={(e) => setSelectedDevice(e.target.value)}
              className="flex-1 rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="">Select a device to add...</option>
              {availableDevices.map((device) => (
                <option key={device.id} value={device.id}>
                  {device.id} — {device.name} ({device.status})
                </option>
              ))}
            </select>
            <button
              type="submit"
              disabled={!selectedDevice}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Add Device
            </button>
          </form>
        )}
      </div>

      {/* Members */}
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-zinc-100">
          Members <span className="text-zinc-500 text-sm font-normal">({group.members.length})</span>
        </h3>

        {group.members.length > 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="px-4 py-3 font-medium">User</th>
                  <th className="px-4 py-3 font-medium">User Role</th>
                  <th className="px-4 py-3 font-medium">Group Role</th>
                  <th className="px-4 py-3 font-medium">Added</th>
                  <th className="px-4 py-3 font-medium w-20"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {group.members.map((member) => (
                  <tr key={member.id} className="hover:bg-zinc-800/30 transition-colors">
                    <td className="px-4 py-3 text-zinc-200">{member.username}</td>
                    <td className="px-4 py-3">
                      <RoleBadge role={member.user_role} />
                    </td>
                    <td className="px-4 py-3">
                      <RoleBadge role={member.group_role} />
                    </td>
                    <td className="px-4 py-3 text-xs text-zinc-500">{member.added_at}</td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleRemoveMember(member.id)}
                        className="text-xs text-red-400 hover:text-red-300 transition-colors"
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 text-center text-zinc-600 text-sm">
            No members in this group
          </div>
        )}

        {/* Add Member Form */}
        {availableUsers.length > 0 && (
          <form onSubmit={handleAddMember} className="flex items-center gap-3">
            <select
              value={selectedUser}
              onChange={(e) => setSelectedUser(e.target.value)}
              className="flex-1 rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="">Select a user to add...</option>
              {availableUsers.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.username} ({user.role})
                </option>
              ))}
            </select>
            <select
              value={selectedRole}
              onChange={(e) => setSelectedRole(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="viewer">viewer</option>
              <option value="editor">editor</option>
              <option value="owner">owner</option>
            </select>
            <button
              type="submit"
              disabled={!selectedUser}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Add Member
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    degraded: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
    stopped: 'bg-red-500/10 border-red-500/20 text-red-400',
    unknown: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
  };
  const cls = styles[status] || styles.unknown;
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${cls}`}>
      {status}
    </span>
  );
}

function RoleBadge({ role }: { role: string }) {
  const styles: Record<string, string> = {
    admin: 'bg-purple-500/10 border-purple-500/20 text-purple-400',
    owner: 'bg-purple-500/10 border-purple-500/20 text-purple-400',
    editor: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
    viewer: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
    user: 'bg-zinc-500/10 border-zinc-500/20 text-zinc-400',
    agent: 'bg-amber-500/10 border-amber-500/20 text-amber-400',
  };
  const cls = styles[role] || styles.viewer;
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${cls}`}>
      {role}
    </span>
  );
}
