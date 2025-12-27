import { NextResponse } from 'next/server';
import { getDCADistribution } from '@/lib/db';
import { getBotConfig } from '@/lib/bot-config';

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const botId = searchParams.get('botId') || 'ao';
    const config = getBotConfig(botId);

    const distribution = await getDCADistribution(config.dcaCount, botId);
    return NextResponse.json(distribution);
  } catch (error) {
    console.error('Failed to fetch DCA distribution:', error);
    return NextResponse.json(
      { error: 'Failed to fetch DCA distribution' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';
export const revalidate = 0;
