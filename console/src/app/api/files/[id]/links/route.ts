import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';

const REGISTRY_BASE = process.env.REGISTRY_URL || 'http://localhost:7777';

/**
 * Session-authenticated proxy for linking a file to the ticket, task or doc it
 * was attached on (#1103).
 *
 * AttachButton used to POST /api/v1/files/{id}/links straight from the
 * browser. The generic /api/v1/[[...path]] proxy forwards only a
 * caller-supplied Authorization header, and a browser has a session cookie,
 * not a header — so the registry refused every console link and the button's
 * best-effort catch hid it. The file stayed unlinked in the uploader's default
 * group, and the object's readers got "File not found" for the embed.
 *
 * Like /api/files/[id]/raw, this forwards the SESSION'S OWN registry token, so
 * the registry still decides whether this user may link this file to that
 * object.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const session = await getSession();
  if (!session.isLoggedIn || !session.apiToken) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { id } = await params;
  if (!/^\d+$/.test(id)) {
    return NextResponse.json({ error: 'Bad file id' }, { status: 400 });
  }

  try {
    const res = await fetch(`${REGISTRY_BASE}/api/v1/files/${id}/links`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${session.apiToken}`,
        'Content-Type': 'application/json',
      },
      body: await request.text(),
      cache: 'no-store',
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json({ error: 'Registry unreachable', detail: String(error) }, { status: 502 });
  }
}
