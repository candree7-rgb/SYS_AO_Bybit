'use client';

import { useState } from 'react';
import StatsCards from '@/components/stats-cards';
import EquityChart from '@/components/equity-chart';
import TradesTable from '@/components/trades-table';
import TPDistributionChart from '@/components/tp-distribution';
import DCADistributionChart from '@/components/dca-distribution';
import BotTabs from '@/components/bot-tabs';
import { getBotConfig } from '@/lib/bot-config';

export default function Dashboard() {
  const [selectedBot, setSelectedBot] = useState<string>('ao');
  const botConfig = getBotConfig(selectedBot);

  return (
    <main className="min-h-screen bg-background p-4 md:p-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-4xl font-bold mb-2">Systemic AO</h1>
        <p className="text-muted-foreground">Multi-bot trading dashboard</p>
      </div>

      {/* Bot Tabs */}
      <BotTabs selectedBot={selectedBot} onSelectBot={setSelectedBot} />

      {/* Stats Cards - All Time */}
      <div className="mb-8">
        <h2 className="text-2xl font-bold mb-4">All Time Performance</h2>
        <StatsCards botId={selectedBot} />
      </div>

      {/* Equity Chart */}
      <div className="mb-8">
        <EquityChart days={30} />
      </div>

      {/* TP & DCA Distribution */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <TPDistributionChart botId={selectedBot} />
        {botConfig.dcaCount > 0 && <DCADistributionChart botId={selectedBot} />}
      </div>

      {/* Trade History Table */}
      <div className="mb-8">
        <TradesTable botId={selectedBot} />
      </div>

      {/* Footer */}
      <div className="text-center text-sm text-muted-foreground mt-12">
        <p>Trading Dashboard • Last updated: {new Date().toLocaleString()}</p>
      </div>
    </main>
  );
}
