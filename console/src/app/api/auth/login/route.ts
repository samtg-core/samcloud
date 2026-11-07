import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';

const REGISTRY_URL = process.env.REGISTRY_URL || 'http://localhost:7777';

export async function GET() {
  try {
    const session = await getSession();
    if (!session.isLoggedIn || !session.username) {
      return NextResponse.json({ error: 'Not logged in' }, { status: 401 });
    }

    // Validate the session by its own bearer (catches DB wipe / deleted user /
    // revoked token). `GET /users` takes a bearer since scope v1 train 0.
    if (session.apiToken) {
      const res = await fetch(`${REGISTRY_URL}/api/v1/auth/verify`, {
        cache: 'no-store',
        headers: { Authorization: `Bearer ${session.apiToken}` },
      });
      if (res.status === 401) {
        session.destroy();
        return NextResponse.json({ error: 'User no longer exists' }, { status: 401 });
      }
    }

    return NextResponse.json({ username: session.username });
  } catch {
    return NextResponse.json({ error: 'Session error' }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    const { username, password } = await request.json();

    if (!username || !password) {
      return NextResponse.json({ error: 'Username and password required' }, { status: 400 });
    }

    // Authenticate against registry
    const res = await fetch(`${REGISTRY_URL}/api/v1/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      cache: 'no-store',
    });

    if (!res.ok) {
      return NextResponse.json({ error: 'Invalid credentials' }, { status: 401 });
    }

    const data = await res.json();

    // Set iron-session — store the registry-issued token so the proxy can forward it
    const session = await getSession();
    session.username = data.username || username.toLowerCase();
    session.isLoggedIn = true;
    session.apiToken = data.token;
    session.conversationId = crypto.randomUUID();   // #886: a login is a new chat
    session.threadId = undefined;
    session.threadTitle = undefined;
    await session.save();

    return NextResponse.json({ ok: true, username: session.username });
  } catch {
    return NextResponse.json({ error: 'Could not connect to registry' }, { status: 502 });
  }
}
