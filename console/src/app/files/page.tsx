'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useScopeGroups } from '@/lib/useScopeGroups';

interface FileRow {
  id: number;
  name: string;
  content_type: string;
  bytes: number;
  group_scope: string | null;
  owner: string;
  created_at: string;
  links: { id: number; target_type: string; target_id: string }[];
}

function human(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function FilesPage() {
  const [files, setFiles] = useState<FileRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<number | null>(null);
  // "" = commons. Applied to every file in the next upload (#328).
  const [scope, setScope] = useState('');
  const groups = useScopeGroups();
  const inputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/files', { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status === 401 ? 'Not signed in' : `Registry ${res.status}`);
      const data = await res.json();
      setFiles(data.files || []);
      setError(null);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function upload(list: FileList | null) {
    if (!list || !list.length) return;
    setBusy(true);
    setError(null);
    try {
      for (const f of Array.from(list)) {
        const body = new FormData();
        body.append('file', f);
        if (scope) body.append('group_scope', scope);
        const res = await fetch('/api/files', { method: 'POST', body });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          // Surface the registry's own reason — 413 and 415 are informative and
          // a generic "upload failed" would throw that away.
          throw new Error(d.detail || d.error || `${f.name}: ${res.status}`);
        }
      }
      await load();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  async function copyMarkdown(f: FileRow) {
    // Canonical /api/v1/ path — agents need bearer-auth URLs in plane content
    // (#487), and Markdown.tsx rewrites v1 → the cookie proxy for console
    // rendering, so browser embeds still resolve.
    const md = `![${f.name}](/api/v1/files/${f.id}/raw)`;
    try {
      await navigator.clipboard.writeText(md);
      setCopied(f.id);
      setTimeout(() => setCopied((c) => (c === f.id ? null : c)), 1500);
    } catch { /* clipboard blocked; the text is visible in the row anyway */ }
  }

  async function remove(f: FileRow) {
    if (!confirm(`Delete "${f.name}"?\n\nAny doc or ticket body embedding it will be left with a broken image — links are structural, markdown in stored bodies is not rewritten.`)) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/v1/files/${f.id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`Delete failed (${res.status})`);
      await load();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-4xl mx-auto p-8">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold text-zinc-100">Files</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Images, video, audio and documents. Upload once, embed in any doc or ticket —
          files are their own thing, so deleting a doc never deletes its images.
        </p>
      </header>

      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); upload(e.dataTransfer.files); }}
        className="border border-dashed border-zinc-700 rounded-lg p-6 text-center mb-6 bg-zinc-900/40"
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => upload(e.target.files)}
        />
        <div className="flex items-center justify-center gap-3">
          <button
            onClick={() => inputRef.current?.click()}
            disabled={busy}
            className="px-4 py-2 rounded-md bg-zinc-100 text-zinc-900 text-sm font-medium disabled:opacity-50"
          >
            {busy ? 'Uploading…' : 'Choose files'}
          </button>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            title="Visibility scope applied to the next upload"
            className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-2 text-xs text-zinc-300 focus:border-zinc-500 focus:outline-none"
          >
            <option value="">commons (all agents)</option>
            {groups.map((g) => <option key={g} value={g}>group: {g}</option>)}
          </select>
        </div>
        <div className="text-xs text-zinc-500 mt-2">or drop them here · up to 1 GB</div>
      </div>

      {error && (
        <div className="mb-4 text-sm text-amber-400 border border-amber-900/50 bg-amber-950/30 rounded-md px-3 py-2">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-center text-zinc-600 text-sm">Loading…</div>
      ) : files.length === 0 ? (
        <div className="text-center text-zinc-600 text-sm py-10">Nothing uploaded yet.</div>
      ) : (
        <ul className="space-y-2">
          {files.map((f) => (
            <li key={f.id} className="flex items-center gap-3 border border-zinc-800 rounded-lg p-3 bg-zinc-900/40">
              <div className="w-14 h-14 shrink-0 rounded bg-zinc-950 overflow-hidden flex items-center justify-center">
                {f.content_type.startsWith('image/') ? (
                  // Proves the proxy works: if this thumbnail renders, embeds render.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={`/api/files/${f.id}/raw`} alt={f.name} className="w-full h-full object-cover" />
                ) : (
                  <span className="text-[10px] text-zinc-600 px-1 text-center break-all">
                    {f.content_type.split('/')[1]?.slice(0, 6) || 'file'}
                  </span>
                )}
              </div>

              <div className="min-w-0 flex-1">
                <div className="text-sm text-zinc-200 truncate">{f.name}</div>
                <div className="text-xs text-zinc-500 tabular-nums">
                  #{f.id} · {human(f.bytes)} · {f.content_type}
                  {f.group_scope ? ` · ${f.group_scope}` : ' · commons'}
                  {f.links.length ? ` · linked ×${f.links.length}` : ''}
                </div>
              </div>

              <button
                onClick={() => copyMarkdown(f)}
                className="text-xs px-2 py-1 rounded border border-zinc-700 text-zinc-300 hover:border-zinc-500"
                title="Copy a markdown embed that works in doc and ticket bodies"
              >
                {copied === f.id ? 'Copied' : 'Copy embed'}
              </button>
              <a
                href={`/api/files/${f.id}/raw`}
                target="_blank"
                rel="noreferrer"
                className="text-xs px-2 py-1 rounded border border-zinc-700 text-zinc-300 hover:border-zinc-500"
              >
                Open
              </a>
              <button
                onClick={() => remove(f)}
                disabled={busy}
                className="text-xs px-2 py-1 rounded border border-zinc-800 text-zinc-500 hover:border-red-900 hover:text-red-400 disabled:opacity-50"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
