import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';
import http from 'node:http';
import https from 'node:https';

const REGISTRY_BASE = process.env.REGISTRY_URL || 'http://localhost:7777';

/** List files visible to the logged-in user. */
export async function GET(request: NextRequest) {
  const session = await getSession();
  if (!session.isLoggedIn || !session.apiToken) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const qs = request.nextUrl.search || '';
  try {
    const res = await fetch(`${REGISTRY_BASE}/api/v1/files${qs}`, {
      headers: { Authorization: `Bearer ${session.apiToken}` },
      cache: 'no-store',
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (error) {
    return NextResponse.json({ error: 'Registry unreachable', detail: String(error) }, { status: 502 });
  }
}

/**
 * Upload, streaming the multipart body through to the registry untouched.
 *
 * The raw body is proxied with its original Content-Type (multipart boundary
 * intact) — never materialized via request.formData(), which buffers the whole
 * file through Node's parser and dies on large videos well under the
 * registry's own 800MB cap (#378). Field validation (`file` present,
 * group_scope) is the registry's job; its errors pass through verbatim.
 * Using the session's own token keeps the registry's scope checks meaningful.
 */
export async function POST(request: NextRequest) {
  const session = await getSession();
  if (!session.isLoggedIn || !session.apiToken) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const contentType = request.headers.get('content-type') || '';
  if (!contentType.includes('multipart/form-data')) {
    return NextResponse.json({ error: 'multipart/form-data required' }, { status: 400 });
  }
  if (!request.body) {
    return NextResponse.json({ error: 'empty body' }, { status: 400 });
  }
  // node:http instead of fetch: undici's fetch forces chunked encoding on
  // stream bodies (conflicting with a forwarded content-length) and applies a
  // ~5min headers timeout that a large upload cannot survive. Plain
  // http.request streams with correct length semantics and no client timeout.
  try {
    const target = new URL(`${REGISTRY_BASE}/api/v1/files`);
    const headers: Record<string, string> = {
      authorization: `Bearer ${session.apiToken}`,
      'content-type': contentType,
    };
    const contentLength = request.headers.get('content-length');
    if (contentLength) headers['content-length'] = contentLength;

    const lib = target.protocol === 'https:' ? https : http;
    const responsePromise = new Promise<{ status: number; body: string }>((resolve, reject) => {
      const upstream = lib.request(
        target,
        { method: 'POST', headers },
        (res) => {
          const chunks: Buffer[] = [];
          res.on('data', (c: Buffer) => chunks.push(c));
          res.on('end', () =>
            resolve({ status: res.statusCode || 502, body: Buffer.concat(chunks).toString('utf8') }));
          res.on('error', reject);
        },
      );
      upstream.on('error', reject);
      // Explicit pump instead of Readable.fromWeb().pipe(): the fromWeb/pipe
      // bridge deadlocks on backpressure past a few MB (1KB passed, 10MB hung
      // in prod bisection). Manual reads + drain-awaits cannot deadlock.
      (async () => {
        const reader = request.body!.getReader();
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (!upstream.write(value)) {
              await new Promise<void>((r) => upstream.once('drain', r));
            }
          }
          upstream.end();
        } catch (err) {
          upstream.destroy(err as Error);
          reject(err);
        }
      })();
    });
    const result = await responsePromise;

    let payload: unknown;
    try { payload = JSON.parse(result.body); }
    catch { payload = { error: 'Registry returned non-JSON', detail: result.body.slice(0, 500) }; }
    return NextResponse.json(payload, { status: result.status });
  } catch (error) {
    return NextResponse.json({ error: 'Upload failed', detail: String(error) }, { status: 502 });
  }
}
