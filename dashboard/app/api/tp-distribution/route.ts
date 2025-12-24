import { NextResponse } from 'next/server';
import { getTPDistribution } from '@/lib/db';

export async function GET() {
  try {
    const distribution = await getTPDistribution();
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
