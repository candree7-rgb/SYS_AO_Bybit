'use client';

import { useEffect, useState } from 'react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { DailyEquity } from '@/lib/db';
import { formatCurrency } from '@/lib/utils';
import { format } from 'date-fns';
import TimeRangeSelector, { TimeRange, TIME_RANGES } from '@/components/time-range-selector';
import DateRangePicker from '@/components/date-range-picker';

interface EquityChartProps {
  botId?: string;
  timeframe?: string;
}

export default function EquityChart({ botId = 'all', timeframe }: EquityChartProps) {
  const [data, setData] = useState<DailyEquity[]>([]);
  const [loading, setLoading] = useState(true);
  const [timeRange, setTimeRange] = useState<TimeRange>('1M');
  const [showDatePicker, setShowDatePicker] = useState(false);
  const [customDateRange, setCustomDateRange] = useState<{ from: string; to: string } | null>(null);

  useEffect(() => {
    async function fetchEquity() {
      try {
        const params = new URLSearchParams();

        // Custom date range takes priority
        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from);
          params.append('to', customDateRange.to);
        } else {
          const selectedRange = TIME_RANGES.find(r => r.value === timeRange);
          const days = selectedRange?.days;
          if (days) params.append('days', days.toString());
        }

        if (botId && botId !== 'all') params.append('botId', botId);
        if (timeframe && timeframe !== 'all') params.append('timeframe', timeframe);

        const res = await fetch(`/api/equity?${params.toString()}`);
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
  }, [timeRange, customDateRange, botId, timeframe]);

  const handleCustomDateApply = (from: string, to: string) => {
    setCustomDateRange({ from, to });
    setTimeRange('CUSTOM');
  };

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

  const displayLabel = timeRange === 'CUSTOM' && customDateRange
    ? `${format(new Date(customDateRange.from), 'MMM dd, yyyy')} - ${format(new Date(customDateRange.to), 'MMM dd, yyyy')}`
    : timeRange;

  return (
    <>
      <DateRangePicker
        isOpen={showDatePicker}
        onClose={() => setShowDatePicker(false)}
        onApply={handleCustomDateApply}
      />

      <div className="bg-card border border-border rounded-lg p-6">
        <div className="flex flex-col gap-4 mb-6">
          {/* Header with Title and Time Range Selector */}
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
            <div>
              <h2 className="text-xl font-bold">Equity Curve</h2>
              {timeRange === 'CUSTOM' && customDateRange && (
                <p className="text-sm text-muted-foreground mt-1">
                  {format(new Date(customDateRange.from), 'MMM dd, yyyy')} - {format(new Date(customDateRange.to), 'MMM dd, yyyy')}
                </p>
              )}
            </div>
            <TimeRangeSelector
              selected={timeRange}
              onSelect={setTimeRange}
              onCustomClick={() => setShowDatePicker(true)}
            />
          </div>

        {/* Equity Stats */}
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
    </>
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
