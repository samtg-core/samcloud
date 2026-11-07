'use client';

import { useState, useEffect, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';

function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
      className="shrink-0 rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700 transition-colors"
    >
      {copied ? 'Copied' : label || 'Copy'}
    </button>
  );
}

function PairForm() {
  const searchParams = useSearchParams();
  const prefilledDevice = searchParams.get('device') || '';
  const [sessionUser, setSessionUser] = useState<string | null>(null);
  const [username, setUsername] = useState('');
  const [deviceName, setDeviceName] = useState(prefilledDevice);
  const [pairing, setPairing] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{
    key_id: string;
    private_key: string;
    public_key: string;
    comment: string;
  } | null>(null);
  const [downloaded, setDownloaded] = useState(false);
  const [showBrowser, setShowBrowser] = useState(false);
  const [link, setLink] = useState<{ pair_command: string; expires_in: number } | null>(null);
  const [linking, setLinking] = useState(false);

  useEffect(() => {
    fetch('/api/auth/login')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.username) {
          setSessionUser(data.username);
          setUsername(data.username);
        }
      })
      .catch(() => {});
  }, []);

  const effectiveUser = sessionUser || username;
  const effectiveDevice = prefilledDevice || deviceName;
  const canGenerate = effectiveUser.trim() && effectiveDevice.trim();

  // A single-use pair link, minted with the signed-in session's bearer. The
  // device that runs it needs no samcloud token of its own (#1056).
  async function makeLink() {
    setError('');
    setLinking(true);
    try {
      const res = await fetch('/api/registry?path=/api/v1/access-keys/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: effectiveUser, device_name: effectiveDevice }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(res.status === 401 ? 'Sign in first.' : data.detail || data.error || 'Failed to make a pair link');
        return;
      }
      setLink(data);
    } catch {
      setError('Could not connect to registry');
    } finally {
      setLinking(false);
    }
  }

  async function handlePair(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setPairing(true);
    try {
      const res = await fetch('/api/registry?path=/api/v1/access-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: effectiveUser, device_name: effectiveDevice }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.detail || data.error || 'Failed to generate keypair');
        return;
      }
      setResult(data);
    } catch {
      setError('Could not connect to registry');
    } finally {
      setPairing(false);
    }
  }

  function downloadKey() {
    if (!result) return;
    const blob = new Blob([result.private_key], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `samcloud_${result.key_id.replace('@', '_')}.key`;
    a.click();
    URL.revokeObjectURL(url);
    setDownloaded(true);
  }

  // ── Result: key generated (browser flow) ───────────────
  if (result) {
    const keyFile = `samcloud_${result.key_id.replace('@', '_')}.key`;
    const sshCmd = `ssh samcloud`;

    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="w-full max-w-lg space-y-5">
          <div className="text-center">
            <h2 className="text-2xl font-bold text-zinc-100">Key Generated</h2>
            <p className="text-zinc-500 mt-1">
              <strong className="text-zinc-200">{result.key_id}</strong>
            </p>
          </div>

          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4">
            <p className="text-sm text-amber-400 font-medium">Save your private key now</p>
            <p className="text-xs text-zinc-500 mt-1">Only shown once.</p>
          </div>

          <button onClick={downloadKey}
            className={`w-full rounded-md px-4 py-3 text-sm font-medium text-white transition-colors ${
              downloaded ? 'bg-emerald-700 hover:bg-emerald-600' : 'bg-blue-600 hover:bg-blue-500'
            }`}>
            {downloaded ? 'Downloaded — download again' : `Download ${keyFile}`}
          </button>

          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs text-zinc-500">Public Key</span>
              <CopyButton text={result.public_key} />
            </div>
            <code className="block text-xs font-mono text-zinc-400 break-all">{result.public_key}</code>
          </div>

          <details className="rounded-lg border border-zinc-800 bg-zinc-900/50">
            <summary className="p-4 text-xs text-zinc-500 cursor-pointer hover:text-zinc-400">
              Private key (click to show)
            </summary>
            <div className="px-4 pb-4 space-y-2">
              <div className="flex justify-end"><CopyButton text={result.private_key} /></div>
              <pre className="text-xs font-mono text-zinc-500 whitespace-pre-wrap break-all max-h-32 overflow-auto">{result.private_key}</pre>
            </div>
          </details>

          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs text-zinc-500">Connect</span>
              <CopyButton text={sshCmd} />
            </div>
            <code className="block text-sm font-mono text-zinc-200">{sshCmd}</code>
            <p className="text-xs text-zinc-600">After installing the key and SSH config on your device.</p>
          </div>
        </div>
      </div>
    );
  }

  // ── Form ───────────────────────────────────────────────
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="w-full max-w-lg space-y-6">
        <div className="text-center">
          <h2 className="text-2xl font-bold text-zinc-100">Pair Device</h2>
          <p className="text-zinc-500 mt-2">
            {prefilledDevice
              ? <>Set up SSH access for <strong className="text-zinc-200">{prefilledDevice}</strong>.</>
              : 'Generate SSH keys to access samcloud.'}
          </p>
        </div>

        {/* Identity fields (only show what's needed) */}
        {(!sessionUser || !prefilledDevice) && (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
            {sessionUser ? (
              <div className="flex items-center gap-2">
                <span className="text-sm text-zinc-400">User:</span>
                <span className="text-sm text-zinc-200">{sessionUser}</span>
              </div>
            ) : (
              <div>
                <label htmlFor="username" className="block text-sm font-medium text-zinc-400 mb-1">Username</label>
                <input id="username" type="text" value={username} onChange={e => setUsername(e.target.value)}
                  className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none text-sm"
                  placeholder="alice" required />
              </div>
            )}
            {!prefilledDevice && (
              <div>
                <label htmlFor="device" className="block text-sm font-medium text-zinc-400 mb-1">Device Name</label>
                <input id="device" type="text" value={deviceName} onChange={e => setDeviceName(e.target.value)}
                  className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-blue-500 focus:outline-none text-sm"
                  placeholder="alices-phone" required />
              </div>
            )}
          </div>
        )}

        {/* Main action: a single-use pair command */}
        {canGenerate && !link && (
          <div className="space-y-3">
            {error && !showBrowser && (
              <div className="rounded-md border border-red-500/20 bg-red-500/5 p-3 text-sm text-red-400">{error}</div>
            )}
            <button onClick={makeLink} disabled={linking}
              className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors">
              {linking ? 'Making link...' : 'Make Pair Command'}
            </button>
          </div>
        )}
        {link && (
          <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-4 space-y-3">
            <p className="text-sm text-blue-400 font-medium">Run on the device you want to connect from:</p>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-sm font-mono text-zinc-200 break-all">{link.pair_command}</code>
              <CopyButton text={link.pair_command} />
            </div>
            <p className="text-xs text-zinc-500">
              Generates a keypair, installs it and configures SSH in one step. Works once, within {Math.round(link.expires_in / 60)} minutes.
            </p>
          </div>
        )}

        {/* Alternative: browser flow */}
        {canGenerate && !showBrowser && (
          <button onClick={() => setShowBrowser(true)}
            className="w-full text-xs text-zinc-500 hover:text-zinc-400 py-2 transition-colors">
            Or generate in browser instead
          </button>
        )}

        {showBrowser && (
          <form onSubmit={handlePair} className="space-y-3">
            {error && (
              <div className="rounded-md border border-red-500/20 bg-red-500/5 p-3 text-sm text-red-400">{error}</div>
            )}
            <button type="submit" disabled={pairing || !canGenerate}
              className="w-full rounded-md bg-zinc-700 px-4 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-600 disabled:opacity-50 transition-colors">
              {pairing ? 'Generating...' : 'Generate in Browser'}
            </button>
            <p className="text-xs text-zinc-600 text-center">
              You&apos;ll need to manually install the key on your device.
            </p>
          </form>
        )}
      </div>
    </div>
  );
}

export default function PairPage() {
  return (
    <Suspense>
      <PairForm />
    </Suspense>
  );
}
