'use client';

import { useRef, useState } from 'react';

/**
 * Upload a file and append a markdown embed to whatever is being composed.
 *
 * Sits next to a textarea rather than in a separate Files page, because the
 * moment you want to attach something is while you are writing the comment —
 * making people leave, upload, copy an embed and come back is the reason
 * "there is an API for it" was not the same as "it is usable".
 */
export function AttachButton({
  onInsert,
  linkTo,
}: {
  onInsert: (markdown: string) => void;
  /** Optionally record a structural link, so the file knows where it is used. */
  linkTo?: { target_type: 'doc' | 'ticket' | 'task'; target_id: string };
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState<string | null>(null);

  async function handle(list: FileList | null) {
    if (!list?.length) return;
    setBusy(true);
    setError(null);
    try {
      let done = 0;
      for (const f of Array.from(list)) {
        setProgress(list.length > 1 ? `${++done}/${list.length}` : '');
        const body = new FormData();
        body.append('file', f);
        const res = await fetch('/api/files', { method: 'POST', body });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // Surface the registry's own reason (413 too big, 415 wrong type) —
          // a generic failure message would throw away the useful part.
          throw new Error(data.detail || data.error || `${f.name}: ${res.status}`);
        }
        const isImage = (data.content_type || '').startsWith('image/');
        // Canonical bearer-auth URL in plane content — agents 401 on the
        // console's cookie proxy (#487). Markdown.tsx rewrites v1 → proxy
        // for console display, so both audiences resolve it.
        onInsert(`${isImage ? '!' : ''}[${data.name}](/api/v1/files/${data.id}/raw)`);

        if (linkTo) {
          // Best-effort: the embed is what the reader sees, the link is
          // bookkeeping. Don't fail the attach if only this part fails.
          fetch(`/api/v1/files/${data.id}/links`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(linkTo),
          }).catch(() => {});
        }
      }
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
      setProgress('');
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  return (
    <span className="inline-flex items-center gap-2">
      <input ref={inputRef} type="file" multiple className="hidden"
             onChange={e => handle(e.target.files)} />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={busy}
        title="Upload files and insert them here — the picker takes several at once (⌘/ctrl-click), and you can attach again after each upload"
        className="text-xs px-2 py-1 rounded border border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-zinc-200 disabled:opacity-50"
      >
        {busy ? `Uploading${progress ? ` ${progress}` : ''}…` : 'Attach files'}
      </button>
      {error && <span className="text-xs text-amber-400">{error}</span>}
    </span>
  );
}
