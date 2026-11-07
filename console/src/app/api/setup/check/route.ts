import { NextResponse } from 'next/server';

const REGISTRY_URL = process.env.REGISTRY_URL || 'http://localhost:7777';

export async function GET() {
  try {
    const res = await fetch(`${REGISTRY_URL}/api/v1/setup/status`, {
      cache: 'no-store',
    });
    if (!res.ok) {
      return NextResponse.json({ initialized: false });
    }
    const status = await res.json();
    return NextResponse.json({ initialized: status?.initialized === true });
  } catch {
    return NextResponse.json({ initialized: false, error: 'Registry offline' });
  }
}
