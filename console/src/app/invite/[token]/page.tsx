'use client';

import { useState, useEffect } from 'react';

interface InviteInfo {
  username: string;
  role: string;
  scopes: string[];
  services: { id: string; status: string; port: number | null }[];
}

const PROXY = '/api/registry';

export default function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const [token, setToken] = useState('');
  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [activating, setActivating] = useState(false);
  const [result, setResult] = useState<{ token: string; username: string } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    params.then(({ token: t }) => {
      setToken(t);
      // Fetch the brief to get invite info
      fetch(`${PROXY}?path=/api/v1/users/brief/${t}`)
        .then(r => {
          if (!r.ok) throw new Error('Invalid or expired invite');
          return r.text();
        })
        .then(md => {
          // Parse basic info from the markdown
          const usernameMatch = md.match(/# Invitation: (.+)/);
          const roleMatch = md.match(/registry as \*\*(.+?)\*\*/);
          const scopeMatches = [...md.matchAll(/- (?:All services on|All services in group|Full admin) \*\*(.+?)\*\*/g)];
          const serviceMatches = [...md.matchAll(/\| `(.+?)` \| `(.+?)` \| `(.+?)` \|/g)];

          setInfo({
            username: usernameMatch?.[1] || 'unknown',
            role: roleMatch?.[1] || 'user',
            scopes: scopeMatches.map(m => m[1]),
            services: serviceMatches.map(m => ({ id: m[1], status: m[2], port: m[3] === 'pending' ? null : parseInt(m[3]) })),
          });
          setLoading(false);
        })
        .catch(e => {
          setError(e.message);
          setLoading(false);
        });
    });
  }, [params]);

  async function handleActivate(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }
    setActivating(true);
    setError('');

    try {
      const body: Record<string, string> = { token };
      if (password) body.password = password;

      const res = await fetch(`${PROXY}?path=/api/v1/users/activate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (res.ok) {
        setResult(data);
      } else {
        setError(data.detail || 'Activation failed');
      }
    } catch {
      setError('Connection error');
    } finally {
      setActivating(false);
    }
  }

  if (loading) return <div className="max-w-lg mx-auto mt-16"><p className="text-zinc-500">Loading invite...</p></div>;
  if (error && !info) return <div className="max-w-lg mx-auto mt-16"><div className="rounded-lg border border-red-500/20 bg-red-500/5 p-6 text-center"><p className="text-red-400">{error}</p></div></div>;

  if (result) {
    return (
      <div className="max-w-lg mx-auto mt-16 space-y-6">
        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-6 space-y-4">
          <h2 className="text-xl font-bold text-emerald-400">Account Activated</h2>
          <p className="text-zinc-300">Welcome, <strong>{result.username}</strong>.</p>
          <p className="text-sm text-zinc-400">
            You can now <a href="/login" className="text-blue-400 hover:text-blue-300">log in to the console</a>.
          </p>
          <div className="space-y-2">
            <p className="text-sm text-zinc-400">Your API token (store securely — shown once):</p>
            <code className="block bg-zinc-900 rounded p-3 text-sm font-mono text-zinc-200 break-all">{result.token}</code>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-lg mx-auto mt-16 space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-zinc-100">You&apos;re Invited</h2>
        <p className="text-zinc-500 mt-1">Set up your samcloud account</p>
      </div>

      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 space-y-4">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-zinc-500">Username</span>
            <p className="font-mono text-zinc-200">{info?.username}</p>
          </div>
          <div>
            <span className="text-zinc-500">Role</span>
            <p className="text-zinc-200">{info?.role}</p>
          </div>
        </div>

        {info?.scopes && info.scopes.length > 0 && (
          <div>
            <span className="text-zinc-500 text-sm">Scope</span>
            <div className="flex gap-2 mt-1">
              {info.scopes.map(s => (
                <span key={s} className="inline-flex items-center rounded-full border border-blue-500/20 bg-blue-500/10 px-2.5 py-0.5 text-xs text-blue-400">{s}</span>
              ))}
            </div>
          </div>
        )}

        {info?.services && info.services.length > 0 && (
          <div>
            <span className="text-zinc-500 text-sm">Existing services in scope</span>
            <div className="mt-1 space-y-1">
              {info.services.map(s => (
                <div key={s.id} className="flex items-center gap-2 text-xs">
                  <span className="font-mono text-zinc-300">{s.id}</span>
                  <span className={`px-1.5 py-0.5 rounded-full border text-xs ${s.status === 'online' ? 'border-emerald-500/20 text-emerald-400' : 'border-zinc-600 text-zinc-400'}`}>{s.status}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <form onSubmit={handleActivate} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6 space-y-4">
        <p className="text-sm text-zinc-400">Set your password. Used for console login and API key rotation.</p>
        <input type="password" placeholder="Password" value={password} onChange={e => setPassword(e.target.value)} required
          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />
        <input type="password" placeholder="Confirm password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} required
          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none" />

        {error && <p className="text-xs text-red-400">{error}</p>}

        <button type="submit" disabled={activating}
          className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors">
          {activating ? 'Activating...' : 'Activate Account'}
        </button>
      </form>
    </div>
  );
}
