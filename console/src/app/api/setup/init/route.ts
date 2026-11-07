import { NextResponse } from 'next/server';

const REGISTRY_URL = process.env.REGISTRY_URL || 'http://localhost:7777';

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const res = await fetch(`${REGISTRY_URL}/api/v1/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });
    const data = await res.json();
    if (!res.ok) {
      return NextResponse.json(
        { status: 'error', error: data.detail || 'Init failed' },
        { status: res.status },
      );
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { status: 'error', error: 'Could not connect to registry' },
      { status: 502 },
    );
  }
}
