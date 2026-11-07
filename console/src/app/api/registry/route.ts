import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';

const REGISTRY_BASE = process.env.REGISTRY_URL || 'http://localhost:7777';

/**
 * Generic proxy to the registry API.
 * Client components use this to avoid CORS and keep registry URL server-side.
 *
 * Auth passthrough: reads the user's registry-issued API token from the
 * iron-session and forwards it as Authorization header. The registry
 * validates and enforces scopes — the console never elevates permissions.
 *
 * Usage: /api/registry?path=/api/v1/status
 *        /api/registry?path=/api/v1/groups/home/status
 */

const CREATE_PATHS = [/^\/api\/v1\/tickets\/?$/, /^\/api\/v1\/docs\/?$/, /^\/api\/v1\/projects\/[^/]+\/tasks\/?$/];

function isCreate(path: string): boolean {
  const bare = path.split('?')[0];
  return CREATE_PATHS.some((re) => re.test(bare));
}

async function proxyRequest(request: NextRequest, method: string) {
  const path = request.nextUrl.searchParams.get('path');
  if (!path) {
    return NextResponse.json({ error: 'Missing ?path= parameter' }, { status: 400 });
  }

  const url = `${REGISTRY_BASE}${path}`;

  // Get the user's API token from session
  const session = await getSession();
  const token = session.apiToken;

  try {
    const body = method !== 'GET' ? await request.text() : undefined;
    const headers: Record<string, string> = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (token) headers['Authorization'] = `Bearer ${token}`;
    // #886: every write is this console conversation's. A create joins the
    // resumed thread when there is one, else the conversation's own thread.
    if (method !== 'GET' && token) {
      if (!session.conversationId) {
        session.conversationId = crypto.randomUUID();
        await session.save();
      }
      headers['X-Session-Id'] = session.conversationId;
      if (session.threadId) headers['X-Thread-Id'] = String(session.threadId);
    }
    const res = await fetch(url, {
      method,
      headers,
      body,
      cache: 'no-store',
    });

    const contentType = res.headers.get('content-type') || '';

    // SSE: stream the response body through without buffering
    if (contentType.includes('text/event-stream') || request.nextUrl.searchParams.get('stream')) {
      return new NextResponse(res.body, {
        status: res.status,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        },
      });
    }

    if (contentType.includes('text/markdown') || contentType.includes('text/plain')) {
      const text = await res.text();
      return new NextResponse(text, {
        status: res.status,
        headers: { 'Content-Type': contentType },
      });
    }
    const data = await res.json().catch(() => ({}));
    // The conversation's first create started its thread: show it as current.
    if (method === 'POST' && res.ok && !session.threadId && isCreate(path)
        && typeof data?.thread_id === 'number') {
      session.threadId = data.thread_id;
      session.threadTitle = data.summary || data.title || undefined;
      await session.save();
    }
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: 'Registry unreachable', detail: String(error) },
      { status: 502 },
    );
  }
}

export async function GET(request: NextRequest) {
  return proxyRequest(request, 'GET');
}

export async function POST(request: NextRequest) {
  return proxyRequest(request, 'POST');
}

export async function PATCH(request: NextRequest) {
  return proxyRequest(request, 'PATCH');
}

export async function DELETE(request: NextRequest) {
  return proxyRequest(request, 'DELETE');
}
