import { NextResponse } from 'next/server'
import { getDailyEquity } from '@/lib/db'

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const daysParam = searchParams.get('days')
    const days = daysParam ? parseInt(daysParam) : undefined
    const from = searchParams.get('from') || undefined
    const to = searchParams.get('to') || undefined
    const equity = await getDailyEquity(days, from, to)
    return NextResponse.json(equity)
  } catch (error) {
    console.error('Failed to fetch equity:', error)
    return NextResponse.json({ error: 'Failed to fetch equity' }, { status: 500 })
  }
}

export const dynamic = 'force-dynamic'
export const revalidate = 0
