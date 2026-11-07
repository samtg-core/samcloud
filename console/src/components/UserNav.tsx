'use client';

import { useState, useEffect } from 'react';

export default function UserNav() {
  const [username, setUsername] = useState<string | null>(null);

  useEffect(() => {
    fetch('/api/auth/login')
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.username) setUsername(d.username); })
      .catch(() => {});
  }, []);

  async function handleLogout() {
    await fetch('/api/auth/logout', { method: 'POST' });
    setUsername(null);
    window.location.href = '/login';
  }

  if (!username) {
    return <a href="/login" className="text-zinc-500 hover:text-white whitespace-nowrap">Login</a>;
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-zinc-500 text-xs">{username}</span>
      <button onClick={handleLogout}
        className="text-zinc-500 hover:text-white whitespace-nowrap text-sm">
        Logout
      </button>
    </div>
  );
}
