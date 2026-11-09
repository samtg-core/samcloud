'use client';

import { useState, useSyncExternalStore } from 'react';
import { useParams } from 'next/navigation';

// The address a pair link's QR (and its short code) opens on the new device.
// Showing the command spends nothing; running it mints the access key, once.
// The page needs no session: the device has none yet (#1068).
const CODE = /^[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}$/;

export default function PairCodePage() {
  const { code } = useParams<{ code: string }>();
  const origin = useSyncExternalStore(() => () => {}, () => window.location.origin, () => '');
  const [copied, setCopied] = useState(false);

  const valid = CODE.test(code || '');
  const command = `curl -fsS ${origin}/api/v1/pair/${(code || '').toUpperCase()} | bash`;

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="w-full max-w-lg space-y-5">
        <div className="text-center">
          <h2 className="text-2xl font-bold text-zinc-100">Pair this device</h2>
          <p className="text-zinc-500 mt-2">Copy the command and run it in a terminal on this device.</p>
        </div>
        {!valid ? (
          <div className="rounded-md border border-red-500/20 bg-red-500/5 p-3 text-sm text-red-400">
            That is not a pair code. Make a new one on the console&apos;s /pair page.
          </div>
        ) : origin && (
          <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-4 space-y-3">
            <code className="block text-sm font-mono text-zinc-200 break-all select-all">{command}</code>
            <button
              onClick={() => { navigator.clipboard.writeText(command); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
              className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 transition-colors">
              {copied ? 'Copied' : 'Copy command'}
            </button>
            <p className="text-xs text-zinc-500">
              It generates a key on this device, installs it and configures SSH, then <code>ssh samcloud</code> works.
              It runs once, within an hour of the link being made.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
