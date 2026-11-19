'use client';

import { useState, useSyncExternalStore } from 'react';
import { useParams } from 'next/navigation';

// The address an access target's QR opens on the new device (#1110), as /p/<code> is for a route.
// Showing the command spends nothing; running it bootstraps the device, once, within the token's hour.
// The page needs no session: the device has none yet.
const TOKEN = /^sc_enroll_[A-Za-z0-9_-]+$/;

export default function BootstrapCodePage() {
  const { token } = useParams<{ token: string }>();
  const origin = useSyncExternalStore(() => () => {}, () => window.location.origin, () => '');
  const [copied, setCopied] = useState(false);

  const valid = TOKEN.test(token || '');
  const command = `curl -s ${origin}/api/v1/bootstrap/${token} | bash`;

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="w-full max-w-lg space-y-5">
        <div className="text-center">
          <h2 className="text-2xl font-bold text-zinc-100">Bootstrap this device</h2>
          <p className="text-zinc-500 mt-2">Copy the command and run it in a terminal on this device.</p>
        </div>
        {!valid ? (
          <div className="rounded-md border border-red-500/20 bg-red-500/5 p-3 text-sm text-red-400">
            That is not a bootstrap link. Enrol the device again on the console&apos;s Devices page.
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
              It sets up the reverse tunnel and activates the device, then the gateway can reach it.
              It runs once, within an hour of the device being enrolled.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
