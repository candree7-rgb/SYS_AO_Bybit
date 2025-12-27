import { NextResponse } from 'next/server';
import { getTPDistribution } from '@/lib/db';
import { getBotConfig } from '@/lib/bot-config';

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const botId = searchParams.get('botId') || 'ao';
    const config = getBotConfig(botId);

    const distribution = await getTPDistribution(config.tpCount, botId);
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
