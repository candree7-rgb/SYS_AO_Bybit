import { NextResponse } from 'next/server';
import { getTPDistribution } from '@/lib/db';
import { getBotConfig } from '@/lib/bot-config';

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const botId = searchParams.get('botId') || 'all';
    const timeframe = searchParams.get('timeframe') || undefined;
    const config = getBotConfig(botId);

    // When showing "all" bots, don't filter by botId in the query
    const filterBotId = botId === 'all' ? undefined : botId;
    const distribution = await getTPDistribution(config.tpCount, filterBotId, timeframe);
    return NextResponse.json(distribution);
  } catch (error) {
    console.error('Failed to fetch TP distribution:', error);
    return NextResponse.json(
      { error: 'Failed to fetch TP distribution' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';
export const revalidate = 0;
