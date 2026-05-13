'use client'

import { useEffect, useState, useMemo } from 'react'
import { Trade } from '@/lib/db'
import { formatCurrency, formatDate, formatDuration, cn } from '@/lib/utils'
import { TimeRange, TIME_RANGES } from './time-range-selector'
import { SimSettings, runSimulation, filterSinglePerBatch } from '@/lib/simulation'

interface TradesTableProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
  simSettings: SimSettings
  isSimulated?: boolean
}

type BadgeVariant = 'tp' | 'trail' | 'be' | 'sl' | 'neutral' | 'update'

function getExitBadges(trade: Trade): { label: string; variant: BadgeVariant }[] {
  const reason = (trade.close_reason || '').toLowerCase()
  const badges: { label: string; variant: BadgeVariant }[] = []

  // UPDATE trades: show close_reason as info badge
  if (trade.side === 'update') {
    const label = trade.close_reason?.trim() || 'CORRECTED'
    badges.push({ label: label.length > 20 ? label.slice(0, 20) + '…' : label, variant: 'update' })
    return badges
  }

  // Parse highest TP level from close_reason
  const tpMatch = reason.match(/tp(\d)/)
  const tpLevel = tpMatch ? parseInt(tpMatch[1]) : 0

  if (reason.includes('trail')) {
    // Trailing stop exit
    if (tpLevel >= 1) {
      badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
    }
    badges.push({ label: 'TRAIL', variant: 'trail' })
  } else if (reason.includes('sl') || reason.includes('stop')) {
    // Stop loss exit
    if (tpLevel >= 1) {
      // TP was hit but SL triggered later = breakeven area
      badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
      badges.push({ label: 'BE', variant: 'be' })
    } else if (trade.tp1_hit) {
      badges.push({ label: 'TP1', variant: 'tp' })
      badges.push({ label: 'BE', variant: 'be' })
    } else {
      badges.push({ label: 'SL', variant: 'sl' })
    }
  } else if (reason.includes('be')) {
    // BE-trail exit
    if (trade.tp1_hit) {
      badges.push({ label: 'TP1', variant: 'tp' })
    }
    badges.push({ label: 'BE', variant: 'be' })
  } else if (reason.includes('neo')) {
    // Neo cloud exit
    if (tpLevel >= 1) {
      badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
    }
    badges.push({ label: 'Flip', variant: 'neutral' })
  } else if (reason.includes('manual') || reason.includes('tg')) {
    badges.push({ label: 'MANUAL', variant: 'neutral' })
  } else if (reason.includes('sync')) {
    badges.push({ label: 'SYNC', variant: 'neutral' })
  } else if (tpLevel >= 1) {
    // Generic TP exit
    badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
  } else if (trade.tp1_hit) {
    badges.push({ label: 'TP1', variant: 'tp' })
  } else {
    badges.push({ label: reason.replace(/_/g, ' ').toUpperCase().slice(0, 8) || '-', variant: 'neutral' })
  }

  return badges
}

const badgeColors: Record<BadgeVariant, string> = {
  tp: 'bg-success/20 text-success',
  trail: 'bg-blue-500/20 text-blue-400',
  be: 'bg-warning/20 text-warning',
  sl: 'bg-danger/20 text-danger',
  neutral: 'bg-muted text-muted-foreground',
  update: 'bg-blue-500/20 text-blue-400 italic',
}

export default function TradesTable({ timeRange, customDateRange, simSettings, isSimulated = true }: TradesTableProps) {
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)

  // Apply batch filter, then run simulation
  const filteredTrades = useMemo(() => {
    return simSettings.singlePerBatch ? filterSinglePerBatch(trades) : trades
  }, [trades, simSettings.singlePerBatch])

  const simResults = useMemo(() => {
    if (!simSettings || filteredTrades.length === 0) return null
    return runSimulation(filteredTrades, simSettings)
  }, [filteredTrades, simSettings])

  useEffect(() => {
    async function fetchTrades() {
      try {
        const params = new URLSearchParams({ limit: '50' })

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
          console.error('Trades API returned', res.status)
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
    const interval = setInterval(fetchTrades, 30000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange, simSettings.excludeWeekends])

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4 animate-pulse"></div>
        <div className="space-y-2">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-16 bg-muted rounded animate-pulse"></div>
          ))}
        </div>
      </div>
    )
  }

  if (filteredTrades.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Trade History</h2>
        <div className="text-center text-muted-foreground py-8">
          No trades found for this period
        </div>
      </div>
    )
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="p-6 pb-4">
        <h2 className="text-xl font-bold">Trade History</h2>
        <p className="text-sm text-muted-foreground mt-1">Last {filteredTrades.filter(t => t.side !== 'update').length} trades</p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="border-y border-border bg-muted/30">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Symbol</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Time</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Side</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Entry</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Duration</th>
              {isSimulated ? (
                <>
                  {simResults && (
                    <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">P&L</th>
                  )}
                </>
              ) : (
                <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">P&L</th>
              )}
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">P&L %</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Exit</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {filteredTrades.map((trade) => (
              <tr key={trade.trade_id} className={cn(
                'hover:bg-muted/20 transition-colors',
                trade.side === 'update' && 'border-l-2 border-l-blue-500 bg-blue-500/5'
              )}>
                {/* Symbol */}
                <td className="px-4 py-4">
                  <div className="flex items-center gap-2">
                    <span className={cn(
                      'font-mono font-semibold',
                      trade.side === 'update' && 'text-blue-400 italic'
                    )}>
                      {trade.symbol.replace('USDT', '')}
                    </span>
                    {trade.side !== 'update' && (
                      <span className="px-1.5 py-0.5 rounded text-xs font-semibold bg-muted/80 text-muted-foreground">
                        {trade.leverage}x
                      </span>
                    )}
                  </div>
                </td>

                {/* Time */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {trade.closed_at ? formatDate(trade.closed_at) : '-'}
                </td>

                {/* Side */}
                <td className="px-4 py-4">
                  <span className={cn(
                    'px-2 py-1 rounded text-xs font-semibold',
                    trade.side === 'long'
                      ? 'bg-success/20 text-success'
                      : trade.side === 'update'
                        ? 'bg-blue-500/20 text-blue-400'
                        : 'bg-danger/20 text-danger'
                  )}>
                    {trade.side.toUpperCase()}
                  </span>
                </td>

                {trade.side === 'update' ? (
                  /* UPDATE row: span from Entry through Exit with note text */
                  <td colSpan={5} className="px-4 py-4">
                    <span className="text-sm text-muted-foreground italic">
                      {trade.close_reason?.trim() || 'Strategy update'}
                    </span>
                  </td>
                ) : (
                  <>
                    {/* Entry */}
                    <td className="px-4 py-4 font-mono text-sm">
                      ${parseFloat(trade.entry_price?.toString() || '0').toFixed(4)}
                    </td>

                    {/* Duration */}
                    <td className="px-4 py-4 text-sm text-muted-foreground">
                      {formatDuration(trade.duration_minutes)}
                    </td>

                    {/* P&L $: simulated mode shows sim P&L, real mode shows account P&L */}
                    {isSimulated ? (
                      <>
                        {simResults && (() => {
                          const sim = simResults.per_trade.get(trade.trade_id)
                          return (
                            <td className="px-4 py-4">
                              {sim ? (
                                <span className={cn(
                                  'font-semibold',
                                  sim.sim_pnl >= 0 ? 'text-success' : 'text-danger'
                                )}>
                                  {sim.sim_pnl >= 0 ? '+' : ''}{formatCurrency(sim.sim_pnl)}
                                </span>
                              ) : (
                                <span className="text-muted-foreground">-</span>
                              )}
                            </td>
                          )
                        })()}
                      </>
                    ) : (
                      <td className="px-4 py-4">
                        <span className={cn(
                          'font-semibold',
                          (trade.realized_pnl || 0) >= 0 ? 'text-success' : 'text-danger'
                        )}>
                          {(trade.realized_pnl || 0) >= 0 ? '+' : ''}
                          {formatCurrency(parseFloat(trade.realized_pnl?.toString() || '0'))}
                        </span>
                      </td>
                    )}

                    {/* P&L % - simulated mode uses scaled %, real mode uses raw DB value */}
                    <td className="px-4 py-4">
                      {(() => {
                        const sim = isSimulated && simResults ? simResults.per_trade.get(trade.trade_id) : null
                        const pct = sim ? sim.sim_pnl_pct : parseFloat(trade.pnl_pct_equity?.toString() || '0')
                        return (
                          <span className={cn(
                            'font-semibold text-sm',
                            pct >= 0 ? 'text-success' : 'text-danger'
                          )}>
                            {pct >= 0 ? '+' : ''}{pct.toFixed(2)}%
                          </span>
                        )
                      })()}
                    </td>

                    {/* Exit badges */}
                    <td className="px-4 py-4">
                      <div className="flex flex-wrap gap-1">
                        {getExitBadges(trade).map((badge, idx) => (
                          <span
                            key={idx}
                            className={cn(
                              'px-2 py-0.5 rounded text-xs font-semibold',
                              badgeColors[badge.variant]
                            )}
                          >
                            {badge.label}
                          </span>
                        ))}
                      </div>
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
