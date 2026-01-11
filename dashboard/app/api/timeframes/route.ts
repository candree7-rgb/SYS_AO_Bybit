import { NextResponse } from 'next/server';
import { getAvailableTimeframes } from '@/lib/db';

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const botId = searchParams.get('botId') || undefined;

    const timeframes = await getAvailableTimeframes(botId);
    return NextResponse.json(timeframes);
  } catch (error) {
    console.error('Failed to fetch timeframes:', error);
    return NextResponse.json(
      { error: 'Failed to fetch timeframes' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';
export const revalidate = 0;
