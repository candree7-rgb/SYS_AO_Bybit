'use client';

import { useEffect, useState } from 'react';
import { Stats } from '@/lib/db';
import { formatCurrency, formatPercent } from '@/lib/utils';

interface StatsCardsProps {
  period?: number; // days, undefined = all time
  botId?: string;
}

export default function StatsCards({ period, botId }: StatsCardsProps) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchStats() {
      try {
        const params = new URLSearchParams();
        if (period) params.append('days', period.toString());
        if (botId) params.append('botId', botId);

        const url = `/api/stats${params.toString() ? `?${params.toString()}` : ''}`;
        const res = await fetch(url);
        const data = await res.json();
        setStats(data);
      } catch (error) {
        console.error('Failed to fetch stats:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchStats();
    const interval = setInterval(fetchStats, 30000); // Refresh every 30s
    return () => clearInterval(interval);
  }, [period, botId]);

  if (loading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {[...Array(8)].map((_, i) => (
          <div key={i} className="bg-card border border-border rounded-lg p-6 animate-pulse">
            <div className="h-4 bg-muted rounded w-1/2 mb-2"></div>
            <div className="h-8 bg-muted rounded w-3/4"></div>
          </div>
        ))}
      </div>
    );
  }

  if (!stats || stats.total_trades === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6 text-center">
        <p className="text-muted-foreground">No trade data available</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      {/* Total Trades */}
      <StatCard
        label="Total Trades"
        value={stats.total_trades.toString()}
        subValue={`${stats.wins}W / ${stats.losses}L`}
      />

      {/* Win Rate */}
      <StatCard
        label="Win Rate"
        value={`${stats.win_rate.toFixed(1)}%`}
        variant={stats.win_rate >= 50 ? 'success' : 'danger'}
      />

      {/* Total Wins */}
      <StatCard
        label="Total Wins"
        value={stats.wins.toString()}
        valueColor="text-success"
      />

      {/* Total Losses */}
      <StatCard
        label="Total Losses"
        value={stats.losses.toString()}
        valueColor="text-danger"
      />

      {/* Total PnL */}
      <StatCard
        label="Total PnL"
        value={formatCurrency(stats.total_pnl)}
        variant={stats.total_pnl >= 0 ? 'success' : 'danger'}
      />

      {/* Avg PnL */}
      <StatCard
        label="Avg PnL"
        value={formatCurrency(stats.avg_pnl)}
        variant={stats.avg_pnl >= 0 ? 'success' : 'danger'}
      />

      {/* Avg Win */}
      <StatCard
        label="Avg Win"
        value={formatCurrency(stats.avg_win)}
        valueColor="text-success"
        subValue={stats.wins > 0 ? `${stats.wins} trades` : undefined}
      />

      {/* Avg Loss */}
      <StatCard
        label="Avg Loss"
        value={formatCurrency(stats.avg_loss)}
        valueColor="text-danger"
        subValue={stats.losses > 0 ? `${stats.losses} trades` : undefined}
      />

      {/* Win/Loss Ratio */}
      <StatCard
        label="Win/Loss Ratio"
        value={`${stats.win_loss_ratio.toFixed(2)}:1`}
        variant={stats.win_loss_ratio >= 1 ? 'success' : 'danger'}
        subValue="R-multiple"
      />

      {/* Best Trade */}
      <StatCard
        label="Best Trade"
        value={formatCurrency(stats.best_trade)}
        valueColor="text-success"
      />

      {/* Worst Trade */}
      <StatCard
        label="Worst Trade"
        value={formatCurrency(stats.worst_trade)}
        valueColor="text-danger"
      />

      {/* Avg TPs Hit */}
      <StatCard
        label="Avg TPs Hit"
        value={stats.avg_tp_fills.toFixed(1)}
        subValue="per trade"
      />

      {/* Avg DCAs Filled */}
      <StatCard
        label="Avg DCAs Filled"
        value={stats.avg_dca_fills.toFixed(1)}
        subValue="per trade"
      />

      {/* Exit Methods */}
      <StatCard
        label="Trailing Exits"
        value={stats.trailing_exits.toString()}
        subValue={`${((stats.trailing_exits / stats.total_trades) * 100).toFixed(0)}% of trades`}
      />

      {/* Stop Loss Exits */}
      <StatCard
        label="Stop Loss Exits"
        value={stats.sl_exits.toString()}
        subValue={`${((stats.sl_exits / stats.total_trades) * 100).toFixed(0)}% of trades`}
      />
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  subValue?: string;
  variant?: 'default' | 'success' | 'danger';
  valueColor?: string;
}

function StatCard({ label, value, subValue, variant = 'default', valueColor }: StatCardProps) {
  let bgClass = 'bg-card';
  let borderClass = 'border-border';
  let textClass = valueColor || 'text-foreground';

  if (variant === 'success') {
    borderClass = 'border-success/20';
    textClass = 'text-success';
  } else if (variant === 'danger') {
    borderClass = 'border-danger/20';
    textClass = 'text-danger';
  }

  return (
    <div className={`${bgClass} border ${borderClass} rounded-lg p-6`}>
      <div className="text-sm text-muted-foreground mb-1">{label}</div>
      <div className={`text-2xl font-bold ${textClass}`}>{value}</div>
      {subValue && <div className="text-xs text-muted-foreground mt-1">{subValue}</div>}
    </div>
  );
}
