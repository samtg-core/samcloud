import { NextRequest, NextResponse } from 'next/server';
import { getSession } from '@/lib/auth';
import {
  getWatchdogHealth,
  getWatchdogIncidents,
  getWatchdogEscalations,
} from '@/lib/watchdog';

export async function GET(request: NextRequest) {
  const session = await getSession();
  if (!session.isLoggedIn) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const type = request.nextUrl.searchParams.get('type') || 'all';

  try {
    if (type === 'health') {
      return NextResponse.json(await getWatchdogHealth());
    }
    if (type === 'incidents') {
      return NextResponse.json(await getWatchdogIncidents());
    }
    if (type === 'escalations') {
      return NextResponse.json(await getWatchdogEscalations());
    }

    // type === 'all'
    const [health, incidents, escalations] = await Promise.all([
      getWatchdogHealth(),
      getWatchdogIncidents(),
      getWatchdogEscalations(),
    ]);

    return NextResponse.json({ health, incidents, escalations });
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to read watchdog state', detail: String(error) },
      { status: 500 },
    );
  }
}
