'use client';

import { useEffect, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts';
import { DCADistribution } from '@/lib/db';

interface DCADistributionChartProps {
  botId?: string;
}

export default function DCADistributionChart({ botId }: DCADistributionChartProps) {
  const [data, setData] = useState<DCADistribution[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchData() {
      try {
        const params = new URLSearchParams();
        if (botId) params.append('botId', botId);

        const url = `/api/dca-distribution${params.toString() ? `?${params.toString()}` : ''}`;
        const res = await fetch(url);
        const distribution = await res.json();
        setData(distribution);
      } catch (error) {
        console.error('Failed to fetch DCA distribution:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchData();
    const interval = setInterval(fetchData, 60000); // Refresh every 60s
    return () => clearInterval(interval);
  }, [botId]);

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/3 mb-4"></div>
        <div className="h-64 bg-muted rounded animate-pulse"></div>
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">DCA Distribution</h2>
        <div className="h-64 flex items-center justify-center text-muted-foreground">
          No DCA data available
        </div>
      </div>
    );
  }

  const chartData = data.map(d => ({
    level: `DCA${d.dca_level}`,
    count: parseInt(d.count.toString()),
    fill: getDCAColor(d.dca_level),
  }));

  const maxCount = Math.max(...chartData.map(d => d.count));

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <h2 className="text-xl font-bold mb-4">DCA Fill Distribution</h2>
      <p className="text-sm text-muted-foreground mb-4">
        How often each DCA level was filled
      </p>

      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" opacity={0.1} />
          <XAxis
            dataKey="level"
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
            domain={[0, maxCount]}
          />
          <Tooltip
            content={<CustomTooltip />}
            cursor={{ fill: 'rgba(255, 255, 255, 0.05)' }}
          />
          <Bar dataKey="count" radius={[8, 8, 0, 0]}>
            {chartData.map((entry, index) => (
              <Cell key={`cell-${index}`} fill={entry.fill} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-border">
        {chartData.map((d) => (
          <div key={d.level} className="text-center">
            <div className="text-xs text-muted-foreground mb-1">{d.level}</div>
            <div className="text-2xl font-bold" style={{ color: d.fill }}>
              {d.count}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function getDCAColor(level: number): string {
  const colors = [
    '#22c55e', // DCA0 - green
    '#f97316', // DCA1 - orange
    '#ef4444', // DCA2 - red
  ];
  return colors[level] || '#6b7280';
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload || !payload[0]) return null;

  const data = payload[0].payload;
  return (
    <div className="bg-card border border-border rounded-lg p-3 shadow-lg">
      <p className="text-sm font-semibold mb-1">{data.level}</p>
      <p className="text-sm text-foreground">
        Filled: <span className="font-bold">{data.count}</span> times
      </p>
    </div>
  );
}
