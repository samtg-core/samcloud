import { NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';

const REGISTRY_URL = process.env.REGISTRY_URL || 'http://localhost:7777';

export async function POST() {
  const session = await getSession();
  // The login's own token ends with it (#1010). Best effort: the cookie goes either way.
  if (session.apiToken) {
    await fetch(`${REGISTRY_URL}/api/v1/auth/logout`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${session.apiToken}` },
      cache: 'no-store',
    }).catch(() => {});
  }
  session.destroy();
  return NextResponse.json({ ok: true });
}
