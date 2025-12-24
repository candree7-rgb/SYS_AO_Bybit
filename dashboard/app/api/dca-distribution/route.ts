import { NextResponse } from 'next/server';
import { getDCADistribution } from '@/lib/db';

export async function GET() {
  try {
    const distribution = await getDCADistribution();
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
