'use client';

import { useEffect, useState } from 'react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { DailyEquity } from '@/lib/db';
import { formatCurrency } from '@/lib/utils';
import { format } from 'date-fns';

interface EquityChartProps {
  days?: number;
}

export default function EquityChart({ days = 30 }: EquityChartProps) {
  const [data, setData] = useState<DailyEquity[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchEquity() {
      try {
        const res = await fetch(`/api/equity?days=${days}`);
        const equity = await res.json();
        setData(equity);
      } catch (error) {
        console.error('Failed to fetch equity:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchEquity();
    const interval = setInterval(fetchEquity, 60000); // Refresh every 60s
    return () => clearInterval(interval);
  }, [days]);

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4"></div>
        <div className="h-64 bg-muted rounded animate-pulse"></div>
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Equity Curve</h2>
        <div className="h-64 flex items-center justify-center text-muted-foreground">
          No equity data available
        </div>
      </div>
    );
  }

  const chartData = data.map(d => ({
    date: format(new Date(d.date), 'MMM dd'),
    equity: parseFloat(d.equity.toString()),
    pnl: parseFloat(d.daily_pnl.toString()),
  }));

  const currentEquity = data[data.length - 1]?.equity || 0;
  const startEquity = data[0]?.equity || 0;
  const totalPnL = currentEquity - startEquity;
  const totalPnLPct = startEquity > 0 ? ((totalPnL / startEquity) * 100) : 0;

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold">Equity Curve</h2>
        <div className="text-right">
          <div className="text-2xl font-bold text-foreground">
            {formatCurrency(currentEquity)}
          </div>
          <div className={`text-sm ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
            {totalPnL >= 0 ? '+' : ''}{formatCurrency(totalPnL)} ({totalPnLPct >= 0 ? '+' : ''}{totalPnLPct.toFixed(2)}%)
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={chartData}>
          <defs>
            <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#3b82f6" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" opacity={0.1} />
          <XAxis
            dataKey="date"
            stroke="#888"
            fontSize={12}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            stroke="#888"
            fontSize={12}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => `$${value.toFixed(0)}`}
          />
          <Tooltip
            content={<CustomTooltip />}
            cursor={{ stroke: '#666', strokeWidth: 1 }}
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="#3b82f6"
            strokeWidth={2}
            fillOpacity={1}
            fill="url(#colorEquity)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload || !payload[0]) return null;

  const data = payload[0].payload;
  return (
    <div className="bg-card border border-border rounded-lg p-3 shadow-lg">
      <p className="text-sm font-semibold mb-1">{data.date}</p>
      <p className="text-sm text-foreground">
        Equity: <span className="font-bold">{formatCurrency(data.equity)}</span>
      </p>
      <p className={`text-sm ${data.pnl >= 0 ? 'text-success' : 'text-danger'}`}>
        Daily P&L: <span className="font-bold">{data.pnl >= 0 ? '+' : ''}{formatCurrency(data.pnl)}</span>
      </p>
    </div>
  );
}
