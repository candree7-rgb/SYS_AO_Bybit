import { NextResponse } from 'next/server'
import { getTrades } from '@/lib/db'

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const limit = parseInt(searchParams.get('limit') || '50')
    const daysParam = searchParams.get('days')
    const days = daysParam ? parseInt(daysParam) : undefined
    const from = searchParams.get('from') || undefined
    const to = searchParams.get('to') || undefined
    const excludeWeekends = searchParams.get('excludeWeekends') === 'true'
    const trades = await getTrades(limit, days, from, to, excludeWeekends)
    return NextResponse.json(trades)
  } catch (error) {
    console.error('Failed to fetch trades:', error)
    return NextResponse.json({ error: 'Failed to fetch trades' }, { status: 500 })
  }
}

export const dynamic = 'force-dynamic'
export const revalidate = 0
