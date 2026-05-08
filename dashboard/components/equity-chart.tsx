'use client'

import { useEffect, useState, useMemo } from 'react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { Trade } from '@/lib/db'
import { formatCurrency } from '@/lib/utils'
import { format } from 'date-fns'
import { TimeRange, TIME_RANGES } from './time-range-selector'
import { SimSettings, runSimulation, filterSinglePerBatch } from '@/lib/simulation'

interface EquityChartProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
  simSettings: SimSettings
  isSimulated?: boolean
}

export default function EquityChart({ timeRange, customDateRange, simSettings, isSimulated = true }: EquityChartProps) {
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function fetchTrades() {
      try {
        const params = new URLSearchParams({ limit: '500' })

        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from)
          params.append('to', customDateRange.to)
        } else {
          const range = TIME_RANGES.find(r => r.value === timeRange)
          if (range?.days) params.append('days', range.days.toString())
        }
        if (simSettings.excludeWeekends) {
          params.append('excludeWeekends', 'true')
        }

        const res = await fetch(`/api/trades?${params.toString()}`)
        if (!res.ok) {
          setTrades([])
          return
        }
        const data = await res.json()
        setTrades(Array.isArray(data) ? data : [])
      } catch (error) {
        console.error('Failed to fetch trades:', error)
      } finally {
        setLoading(false)
      }
    }

    setLoading(true)
    fetchTrades()
    const interval = setInterval(fetchTrades, 60000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange, simSettings.excludeWeekends])

  // Build equity curve from simulation
  const chartData = useMemo(() => {
    const filtered = simSettings.singlePerBatch ? filterSinglePerBatch(trades) : trades
    const realTrades = filtered.filter(t => t.side !== 'update')
    if (realTrades.length === 0) return []

    const sim = runSimulation(realTrades, simSettings)

    // Sort trades chronologically
    const sorted = [...realTrades].sort((a, b) =>
      new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime()
    )

    // Start point
    const points = [{
      date: 'Start',
      fullDate: 'Starting Equity',
      equity: simSettings.equity,
      pnl: 0,
    }]

    // One point per trade
    for (const trade of sorted) {
      const result = sim.per_trade.get(trade.trade_id)
      if (!result) continue
      points.push({
        date: format(new Date(trade.closed_at), 'MMM dd HH:mm'),
        fullDate: format(new Date(trade.closed_at), 'MMM dd, yyyy HH:mm'),
        equity: result.sim_equity_after,
        pnl: result.sim_pnl,
      })
    }

    return points
  }, [trades, simSettings])

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4 animate-pulse"></div>
        <div className="h-64 bg-muted rounded animate-pulse"></div>
      </div>
    )
  }

  if (chartData.length <= 1) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Equity Curve</h2>
        <div className="h-64 flex items-center justify-center text-muted-foreground">
          No trade data available
        </div>
      </div>
    )
  }

  const currentEquity = chartData[chartData.length - 1].equity
  const totalPnL = currentEquity - simSettings.equity
  const totalPnLPct = (totalPnL / simSettings.equity) * 100

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <div className="flex flex-col gap-4 mb-6">
        <h2 className="text-xl font-bold">Equity Curve</h2>

        <div className="flex items-center justify-between p-4 bg-muted/50 rounded-lg">
          <div>
            <div className="text-sm text-muted-foreground mb-1">Current Equity</div>
            <div className="text-3xl font-bold text-foreground">
              {formatCurrency(currentEquity)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-sm text-muted-foreground mb-1">
              {timeRange === 'CUSTOM' ? 'Custom' : timeRange} Performance
            </div>
            <div className={`text-2xl font-bold ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
              {totalPnL >= 0 ? '+' : ''}{formatCurrency(totalPnL)}
            </div>
            <div className={`text-sm ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
              {totalPnLPct >= 0 ? '+' : ''}{totalPnLPct.toFixed(2)}%
            </div>
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={chartData}>
          <defs>
            <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#22c55e" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" opacity={0.1} />
          <XAxis dataKey="date" stroke="#888" fontSize={12} tickLine={false} axisLine={false} />
          <YAxis
            stroke="#888"
            fontSize={12}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => `$${(value ?? 0).toFixed(0)}`}
            domain={['dataMin - 100', 'dataMax + 100']}
          />
          <Tooltip content={<CustomTooltip />} />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="#22c55e"
            strokeWidth={2}
            fillOpacity={1}
            fill="url(#colorEquity)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload || !payload[0]) return null
  const data = payload[0].payload
  return (
    <div className="bg-card border border-border rounded-lg p-3 shadow-lg">
      <p className="text-sm font-semibold mb-1">{data.fullDate}</p>
      <p className="text-sm text-foreground">
        Equity: <span className="font-bold">{formatCurrency(data.equity)}</span>
      </p>
      {data.pnl !== 0 && (
        <p className={`text-sm ${data.pnl >= 0 ? 'text-success' : 'text-danger'}`}>
          Trade P&L: <span className="font-bold">{data.pnl >= 0 ? '+' : ''}{formatCurrency(data.pnl)}</span>
        </p>
      )}
    </div>
  )
}
