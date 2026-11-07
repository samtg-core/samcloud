import { NextRequest, NextResponse } from 'next/server';

const REGISTRY_BASE = process.env.REGISTRY_URL || 'http://localhost:7777';

/**
 * Catch-all proxy for /api/v1/* → registry API.
 * Used by bootstrap scripts and external devices that can't reach the registry directly.
 */
async function proxy(request: NextRequest, { params }: { params: Promise<{ path?: string[] }> }) {
  const { path } = await params;
  const registryPath = `/api/v1/${(path || []).join('/')}`;
  const url = `${REGISTRY_BASE}${registryPath}${request.nextUrl.search}`;

  const body = request.method !== 'GET' ? await request.text() : undefined;
  const headers: Record<string, string> = {};
  if (body) headers['Content-Type'] = 'application/json';
  const auth = request.headers.get('authorization');
  if (auth) headers['Authorization'] = auth;

  try {
    const res = await fetch(url, {
      method: request.method,
      headers,
      body,
      cache: 'no-store',
    });

    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('json')) {
      const data = await res.json();
      return NextResponse.json(data, { status: res.status });
    }
    const text = await res.text();
    return new Response(text, {
      status: res.status,
      headers: { 'Content-Type': contentType },
    });
  } catch (error) {
    return NextResponse.json(
      { error: 'Registry unreachable', detail: String(error) },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
