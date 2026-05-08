import { NextResponse } from 'next/server'
import { getStats } from '@/lib/db'

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
}

export async function OPTIONS() {
  return new NextResponse(null, { status: 204, headers: CORS_HEADERS })
}

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const daysParam = searchParams.get('days')
    const days = daysParam ? parseInt(daysParam) : undefined
    const from = searchParams.get('from') || undefined
    const to = searchParams.get('to') || undefined
    const excludeWeekends = searchParams.get('excludeWeekends') === 'true'
    const stats = await getStats(days, from, to, excludeWeekends)
    return NextResponse.json(stats, { headers: CORS_HEADERS })
  } catch (error) {
    console.error('Failed to fetch stats:', error)
    return NextResponse.json({ error: 'Failed to fetch stats' }, { status: 500, headers: CORS_HEADERS })
  }
}

export const dynamic = 'force-dynamic'
export const revalidate = 0
