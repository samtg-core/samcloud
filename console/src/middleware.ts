import { NextRequest, NextResponse } from 'next/server';

const REGISTRY_URL = process.env.REGISTRY_URL || 'http://localhost:7777';

// Paths that don't require authentication
// '/p/' is a pair code's page, opened on a device with no session (#1068).
const PUBLIC_PATHS = ['/login', '/setup', '/pair', '/p/', '/invite', '/api/'];

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname.startsWith(p));
}

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Allow public paths (login, setup, API routes)
  if (isPublicPath(pathname)) {
    return NextResponse.next();
  }

  // Allow static assets and Next.js internals
  if (
    pathname.startsWith('/_next') ||
    pathname.startsWith('/favicon') ||
    pathname.includes('.')
  ) {
    return NextResponse.next();
  }

  // Check if system is initialized and validate session
  let usersExist = false;
  try {
    // `GET /users` takes a bearer since scope v1 train 0; the initialised
    // fact lives on its own unauthenticated route.
    const res = await fetch(`${REGISTRY_URL}/api/v1/setup/status`, {
      cache: 'no-store',
    });
    if (res.ok) {
      const status = await res.json();
      usersExist = status?.initialized === true;
    }
  } catch {
    // Registry offline — redirect to setup
    return NextResponse.redirect(new URL('/setup', request.url));
  }

  if (!usersExist) {
    return NextResponse.redirect(new URL('/setup', request.url));
  }

  // Session cookie exists — let them through (API routes validate user exists)
  const sessionCookie = request.cookies.get('samcloud-chat');
  if (sessionCookie) {
    return NextResponse.next();
  }

  // No session — redirect to login
  const loginUrl = new URL('/login', request.url);
  loginUrl.searchParams.set('next', pathname);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|api/files).*)'],
};
