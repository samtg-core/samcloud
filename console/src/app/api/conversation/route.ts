import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';

/**
 * The console's conversation (#886). Like an interactive session, a person on
 * the web is one conversation at a time: everything it creates joins one
 * thread. "New" starts a fresh conversation, whose first create starts a
 * thread; "resume" names an existing thread for the creates that follow.
 *
 *   GET                                   → { conversationId, threadId, threadTitle }
 *   POST { action: "new" }                → a new conversation, no thread yet
 *   POST { action: "resume", threadId }   → creates join that thread
 */
export async function GET() {
  const session = await getSession();
  if (!session.isLoggedIn) return NextResponse.json({ error: 'Not logged in' }, { status: 401 });
  if (!session.conversationId) {
    session.conversationId = crypto.randomUUID();
    await session.save();
  }
  return NextResponse.json({
    conversationId: session.conversationId,
    threadId: session.threadId ?? null,
    threadTitle: session.threadTitle ?? null,
  });
}

export async function POST(request: NextRequest) {
  const session = await getSession();
  if (!session.isLoggedIn) return NextResponse.json({ error: 'Not logged in' }, { status: 401 });
  const body = await request.json().catch(() => ({}));
  if (body.action === 'new') {
    session.conversationId = crypto.randomUUID();
    session.threadId = undefined;
    session.threadTitle = undefined;
  } else if (body.action === 'resume' && Number.isInteger(body.threadId) && body.threadId > 0) {
    session.threadId = body.threadId;
    session.threadTitle = typeof body.title === 'string' ? body.title.slice(0, 200) : undefined;
  } else {
    return NextResponse.json({ error: 'action must be "new", or "resume" with a threadId' }, { status: 400 });
  }
  await session.save();
  return NextResponse.json({
    conversationId: session.conversationId,
    threadId: session.threadId ?? null,
    threadTitle: session.threadTitle ?? null,
  });
}
