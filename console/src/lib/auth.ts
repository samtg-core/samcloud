import { getIronSession, IronSession } from 'iron-session';
import { cookies, headers } from 'next/headers';

export interface SessionData {
  username?: string;
  isLoggedIn?: boolean;
  apiToken?: string;  // registry-issued token, forwarded on every API call
  // #886: the console is a conversation, like an interactive session. Every
  // write carries its id; what it creates joins its thread. A new one starts
  // at login or on "new thread"; `threadId` is set when it resumes a thread.
  conversationId?: string;
  threadId?: number;
  threadTitle?: string;
}

// The cookie is Secure when the browser reached the console over HTTPS (#1010).
// Behind the bundled Caddy that is every request, and Caddy sets
// X-Forwarded-Proto itself. A LAN first run reaches the console directly on
// CONSOLE_PORT over plain HTTP, where a browser drops a Secure cookie, so login
// would never stick. SESSION_COOKIE_SECURE=true forces it on for every request.
async function secureCookie(): Promise<boolean> {
  if (process.env.SESSION_COOKIE_SECURE === 'true') return true;
  const proto = (await headers()).get('x-forwarded-proto')?.split(',')[0].trim();
  return proto === 'https';
}

export async function getSession(): Promise<IronSession<SessionData>> {
  const cookieStore = await cookies();
  return getIronSession<SessionData>(cookieStore, {
    password: process.env.AUTH_SECRET!,
    cookieName: 'samcloud-chat',
    cookieOptions: {
      secure: await secureCookie(),
      httpOnly: true,
      sameSite: 'lax' as const,
      maxAge: 60 * 60 * 24 * 7, // 1 week
    },
  });
}
