import { NextResponse } from 'next/server'
import { Pool } from 'pg'
import crypto from 'crypto'

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 5,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 2000,
})

const BYBIT_API_KEY = process.env.BYBIT_API_KEY || ''
const BYBIT_API_SECRET = process.env.BYBIT_API_SECRET || ''
const BYBIT_TESTNET = process.env.BYBIT_TESTNET === 'true'
const BYBIT_DEMO = process.env.BYBIT_DEMO === 'true'

function getBybitBaseUrl(): string {
  if (BYBIT_DEMO) return 'https://api-demo.bybit.com'
  if (BYBIT_TESTNET) return 'https://api-testnet.bybit.com'
  return 'https://api.bybit.com'
}

function generateSignature(timestamp: string, apiKey: string, recvWindow: string, queryString: string): string {
  const message = timestamp + apiKey + recvWindow + queryString
  return crypto.createHmac('sha256', BYBIT_API_SECRET).update(message).digest('hex')
}

interface BybitClosedPnlRecord {
  symbol: string
  side: string // Buy = closed short, Sell = closed long
  qty: string
  avgEntryPrice: string
  avgExitPrice: string
  closedPnl: string
  orderType: string
  createdTime: string
  updatedTime: string
}

async function getBybitClosedPnl(startTimeMs: number): Promise<{ symbol: string; side: string; closedPnl: number }[]> {
  const timestamp = Date.now().toString()
  const recvWindow = '5000'
  const params = new URLSearchParams({
    category: 'linear',
    limit: '50',
  })
  if (startTimeMs > 0) {
    params.append('startTime', startTimeMs.toString())
  }
  const queryString = params.toString()
  const signature = generateSignature(timestamp, BYBIT_API_KEY, recvWindow, queryString)

  const baseUrl = getBybitBaseUrl()
  const url = `${baseUrl}/v5/position/closed-pnl?${queryString}`

  const response = await fetch(url, {
    headers: {
      'X-BAPI-API-KEY': BYBIT_API_KEY,
      'X-BAPI-SIGN': signature,
      'X-BAPI-SIGN-TYPE': '2',
      'X-BAPI-TIMESTAMP': timestamp,
      'X-BAPI-RECV-WINDOW': recvWindow,
      'Content-Type': 'application/json',
    },
  })

  if (!response.ok) {
    throw new Error(`Bybit API returned ${response.status}`)
  }

  const data = await response.json()
  if (data.retCode !== 0) {
    throw new Error(`Bybit error: ${data.retMsg}`)
  }

  const list: BybitClosedPnlRecord[] = data.result?.list || []
  return list.map((r) => ({
    symbol: r.symbol,
    // INVERT: Buy closing order = short position, Sell = long
    side: r.side === 'Buy' ? 'short' : 'long',
    closedPnl: parseFloat(r.closedPnl),
  }))
}

export async function GET() {
  try {
    if (!BYBIT_API_KEY || !BYBIT_API_SECRET) {
      return NextResponse.json({ error: 'Bybit credentials not configured' }, { status: 500 })
    }

    // Get recent trades from DB (skip manually edited 'update' rows)
    const cutoff = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
    const { rows } = await pool.query(
      `SELECT trade_id, symbol, side, total_margin, equity_at_entry,
              realized_pnl, opened_at
       FROM trades
       WHERE opened_at >= $1 AND side != 'update'
       ORDER BY opened_at DESC`,
      [cutoff]
    )

    if (rows.length === 0) {
      return NextResponse.json({ status: 'no trades to fix', fixed: 0 })
    }

    const results: {
      trade_id: string
      symbol: string
      old_pnl: number
      bybit_pnl: number
      diff: number
      updated: boolean
    }[] = []

    for (const row of rows) {
      const openedAt = row.opened_at instanceof Date ? row.opened_at.getTime() : 0
      if (openedAt === 0) continue

      // Query Bybit for closed PnL records
      const records = await getBybitClosedPnl(openedAt)
      const matching = records.filter(
        (r) => r.symbol === row.symbol && r.side === row.side
      )

      if (matching.length === 0) continue

      const bybitPnl = matching.reduce((sum, r) => sum + r.closedPnl, 0)
      const oldPnl = parseFloat(row.realized_pnl || '0')
      const diff = bybitPnl - oldPnl

      if (Math.abs(diff) < 0.001) continue // Already correct

      // Recalculate derived fields
      const totalMargin = parseFloat(row.total_margin || '0')
      const equityAtEntry = parseFloat(row.equity_at_entry || '0')
      const pnlPctMargin = totalMargin > 0 ? (bybitPnl / totalMargin) * 100 : 0
      const pnlPctEquity = equityAtEntry > 0 ? (bybitPnl / equityAtEntry) * 100 : 0
      const isWin = bybitPnl > 0.01
      const equityAtClose = equityAtEntry + bybitPnl

      // Update DB
      const updateResult = await pool.query(
        `UPDATE trades SET
           realized_pnl = $1, pnl_pct_margin = $2, pnl_pct_equity = $3,
           equity_at_close = $4, is_win = $5
         WHERE trade_id = $6`,
        [bybitPnl, pnlPctMargin, pnlPctEquity, equityAtClose, isWin, row.trade_id]
      )

      results.push({
        trade_id: row.trade_id,
        symbol: row.symbol,
        old_pnl: Math.round(oldPnl * 10000) / 10000,
        bybit_pnl: Math.round(bybitPnl * 10000) / 10000,
        diff: Math.round(diff * 10000) / 10000,
        updated: (updateResult.rowCount ?? 0) > 0,
      })
    }

    return NextResponse.json({
      status: 'done',
      fixed: results.length,
      total_checked: rows.length,
      details: results,
    })
  } catch (error) {
    console.error('fix-pnl error:', error)
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unknown error' },
      { status: 500 }
    )
  }
}

export const dynamic = 'force-dynamic'
export const revalidate = 0
