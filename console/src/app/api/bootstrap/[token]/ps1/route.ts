import { NextRequest } from 'next/server';

const REGISTRY_BASE = process.env.REGISTRY_URL || 'http://localhost:7777';

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ token: string }> }
) {
  const { token } = await params;
  const res = await fetch(`${REGISTRY_BASE}/api/v1/bootstrap/${token}/ps1`);

  if (!res.ok) {
    return new Response(await res.text(), { status: res.status });
  }

  const script = await res.text();
  return new Response(script, {
    headers: { 'Content-Type': 'text/plain' },
  });
}
