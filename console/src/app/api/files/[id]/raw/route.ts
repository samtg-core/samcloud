import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';

const REGISTRY_BASE = process.env.REGISTRY_URL || 'http://localhost:7777';

/**
 * Session-authenticated binary proxy for registry files.
 *
 * Why this exists: the registry requires `Authorization: Bearer` on
 * /api/v1/files/{id}/raw, and a browser rendering `![](...)` cannot attach a
 * header. So the documented markdown embed did not render for anyone (found
 * independently by two users on staging).
 *
 * Serving commons files unauthenticated was the tempting fix and the wrong one:
 * the registry is public and file ids are sequential, so that would make every
 * "commons" file world-readable to anyone who guesses a number.
 *
 * Instead the browser sends its iron-session cookie automatically, and we
 * forward the SESSION'S OWN registry token — not a console service token — so
 * the registry still enforces exactly the scope that user has. A proxy that
 * used a privileged token would silently become a scope bypass.
 *
 * Note the generic /api/v1/[[...path]] proxy cannot serve this: it forwards a
 * caller-supplied Authorization header (browsers have none) and reads non-JSON
 * bodies via res.text(), which corrupts binary.
 */
export async function GET(
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
    const res = await fetch(`${REGISTRY_BASE}/api/v1/files/${id}/raw`, {
      headers: { Authorization: `Bearer ${session.apiToken}` },
      cache: 'no-store',
    });

    if (!res.ok) {
      return NextResponse.json(
        { error: 'File not available', status: res.status },
        { status: res.status },
      );
    }

    // The body is passed through as a stream, never materialized: res.text()
    // would mangle a non-UTF8 payload, and res.arrayBuffer() — what this did
    // until #889 — allocates the whole file in the console's heap, which a
    // 1 GB splat capture turns into an OOM inside a 7.7 GB VM that is also
    // running the registry. Binary fidelity is the same either way.
    return new Response(res.body, {
      status: 200,
      headers: {
        'Content-Type': res.headers.get('content-type') || 'application/octet-stream',
        'Content-Disposition': res.headers.get('content-disposition') || 'inline',
        'X-Content-Type-Options': 'nosniff',
        // private: this is scoped per-session, so no shared cache may keep it
        'Cache-Control': 'private, max-age=3600',
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: 'Registry unreachable', detail: String(error) },
      { status: 502 },
    );
  }
}
