'use client';

import { useState } from 'react';
import StatsCards from '@/components/stats-cards';
import EquityChart from '@/components/equity-chart';
import TradesTable from '@/components/trades-table';
import TPDistributionChart from '@/components/tp-distribution';
import DCADistributionChart from '@/components/dca-distribution';
import BotTabs from '@/components/bot-tabs';
import TimeframeSelector from '@/components/timeframe-selector';
import { getBotConfig } from '@/lib/bot-config';

export default function Dashboard() {
  const [selectedBot, setSelectedBot] = useState<string>('all');
  const [selectedTimeframe, setSelectedTimeframe] = useState<string>('all');
  const botConfig = getBotConfig(selectedBot);

  return (
    <main className="min-h-screen bg-background p-4 md:p-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-4xl font-bold mb-2">Systemic*</h1>
        <p className="text-muted-foreground">Multi-bot trading dashboard</p>
      </div>

      {/* Bot Tabs */}
      <BotTabs selectedBot={selectedBot} onSelectBot={setSelectedBot} />

      {/* Timeframe Selector */}
      <div className="mb-6">
        <TimeframeSelector
          selectedTimeframe={selectedTimeframe}
          onSelectTimeframe={setSelectedTimeframe}
          botId={selectedBot}
        />
      </div>

      {/* Stats Cards - All Time */}
      <div className="mb-8">
        <h2 className="text-2xl font-bold mb-4">All Time Performance</h2>
        <StatsCards botId={selectedBot} timeframe={selectedTimeframe} />
      </div>

      {/* Equity Chart */}
      <div className="mb-8">
        <EquityChart botId={selectedBot} timeframe={selectedTimeframe} />
      </div>

      {/* TP & DCA Distribution */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <TPDistributionChart botId={selectedBot} timeframe={selectedTimeframe} />
        {botConfig.dcaCount > 0 && <DCADistributionChart botId={selectedBot} timeframe={selectedTimeframe} />}
      </div>

      {/* Trade History Table */}
      <div className="mb-8">
        <TradesTable botId={selectedBot} timeframe={selectedTimeframe} />
      </div>

      {/* Footer */}
      <div className="text-center text-sm text-muted-foreground mt-12">
        <p>Trading Dashboard • Last updated: {new Date().toLocaleString()}</p>
      </div>
    </main>
  );
}
