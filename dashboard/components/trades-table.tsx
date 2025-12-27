'use client';

import { useEffect, useState } from 'react';
import { Trade } from '@/lib/db';
import { formatCurrency, formatDate, formatDuration, cn } from '@/lib/utils';

interface TradesTableProps {
  botId?: string;
}

export default function TradesTable({ botId }: TradesTableProps) {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [sortField, setSortField] = useState<keyof Trade>('closed_at');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');

  useEffect(() => {
    async function fetchTrades() {
      try {
        const params = new URLSearchParams({ limit: '50' });
        if (botId) params.append('botId', botId);

        const res = await fetch(`/api/trades?${params.toString()}`);
        const data = await res.json();
        setTrades(data);
      } catch (error) {
        console.error('Failed to fetch trades:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchTrades();
    const interval = setInterval(fetchTrades, 30000); // Refresh every 30s
    return () => clearInterval(interval);
  }, [botId]);

  const handleSort = (field: keyof Trade) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  const sortedTrades = [...trades].sort((a, b) => {
    const aVal = a[sortField];
    const bVal = b[sortField];

    if (aVal === null || aVal === undefined) return 1;
    if (bVal === null || bVal === undefined) return -1;

    if (aVal < bVal) return sortDirection === 'asc' ? -1 : 1;
    if (aVal > bVal) return sortDirection === 'asc' ? 1 : -1;
    return 0;
  });

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4"></div>
        <div className="space-y-2">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-16 bg-muted rounded animate-pulse"></div>
          ))}
        </div>
      </div>
    );
  }

  if (trades.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Trade History</h2>
        <div className="text-center text-muted-foreground py-8">
          No trades found
        </div>
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="p-6 pb-4">
        <h2 className="text-xl font-bold">Trade History</h2>
        <p className="text-sm text-muted-foreground mt-1">Last {trades.length} trades</p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="border-y border-border bg-muted/30">
            <tr>
              <TableHeader onClick={() => handleSort('symbol')}>Symbol</TableHeader>
              <TableHeader onClick={() => handleSort('closed_at')}>Close Time</TableHeader>
              <TableHeader onClick={() => handleSort('side')}>Position</TableHeader>
              <TableHeader onClick={() => handleSort('entry_price')}>Entry</TableHeader>
              <TableHeader onClick={() => handleSort('duration_minutes')}>Duration</TableHeader>
              <TableHeader onClick={() => handleSort('realized_pnl')}>P&L</TableHeader>
              <TableHeader onClick={() => handleSort('pnl_pct_equity')}>P&L %</TableHeader>
              <TableHeader onClick={() => handleSort('exit_reason')}>Exit Reason</TableHeader>
              <TableHeader>TPs/DCAs</TableHeader>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {sortedTrades.map((trade) => (
              <tr
                key={trade.id}
                className="hover:bg-muted/20 transition-colors cursor-pointer group"
              >
                {/* Symbol */}
                <td className="px-4 py-4 font-mono font-semibold">
                  {trade.symbol.replace('USDT', '')}
                </td>

                {/* Close Time */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {trade.closed_at ? formatDate(trade.closed_at) : '-'}
                </td>

                {/* Position */}
                <td className="px-4 py-4">
                  <span
                    className={cn(
                      'px-2 py-1 rounded text-xs font-semibold',
                      trade.side === 'Long'
                        ? 'bg-success/20 text-success'
                        : 'bg-danger/20 text-danger'
                    )}
                  >
                    {trade.side}
                  </span>
                </td>

                {/* Entry Price */}
                <td className="px-4 py-4 font-mono text-sm">
                  ${parseFloat(trade.entry_price?.toString() || '0').toFixed(4)}
                </td>

                {/* Duration */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {formatDuration(trade.duration_minutes)}
                </td>

                {/* P&L */}
                <td className="px-4 py-4">
                  <span
                    className={cn(
                      'font-semibold',
                      trade.realized_pnl >= 0 ? 'text-success' : 'text-danger'
                    )}
                  >
                    {trade.realized_pnl >= 0 ? '+' : ''}
                    {formatCurrency(parseFloat(trade.realized_pnl?.toString() || '0'))}
                  </span>
                </td>

                {/* P&L % */}
                <td className="px-4 py-4">
                  <span
                    className={cn(
                      'font-semibold text-sm',
                      trade.pnl_pct_equity >= 0 ? 'text-success' : 'text-danger'
                    )}
                  >
                    {trade.pnl_pct_equity >= 0 ? '+' : ''}
                    {parseFloat(trade.pnl_pct_equity?.toString() || '0').toFixed(2)}%
                  </span>
                </td>

                {/* Exit Reason */}
                <td className="px-4 py-4 text-sm">
                  <span className="capitalize">
                    {trade.exit_reason.replace(/_/g, ' ')}
                  </span>
                </td>

                {/* TPs/DCAs */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  <div className="flex gap-3">
                    <span>
                      TPs: {trade.tp_fills}/{trade.tp_count}
                    </span>
                    <span>
                      DCAs: {trade.dca_fills}/{trade.dca_count}
                    </span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TableHeader({
  children,
  onClick,
}: {
  children: React.ReactNode;
  onClick?: () => void;
}) {
  return (
    <th
      onClick={onClick}
      className={cn(
        'px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider',
        onClick && 'cursor-pointer hover:text-foreground transition-colors'
      )}
    >
      {children}
    </th>
  );
}
