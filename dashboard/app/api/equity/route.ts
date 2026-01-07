import { NextResponse } from 'next/server';
import { getDailyEquity, getBotCumulativePnL } from '@/lib/db';

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const daysParam = searchParams.get('days');
    const days = daysParam ? parseInt(daysParam) : undefined;
    const from = searchParams.get('from') || undefined;
    const to = searchParams.get('to') || undefined;
    const botId = searchParams.get('botId');

    // If specific bot is selected, calculate cumulative PnL from trades
    // If 'all' or no botId, use daily_equity table (account-wide equity)
    const equity = (botId && botId !== 'all')
      ? await getBotCumulativePnL(botId, days, from, to)
      : await getDailyEquity(days, from, to);

    return NextResponse.json(equity);
  } catch (error) {
    console.error('Failed to fetch equity:', error);
    return NextResponse.json(
      { error: 'Failed to fetch equity' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';
export const revalidate = 0;
