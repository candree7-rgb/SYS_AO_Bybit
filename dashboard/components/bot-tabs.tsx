'use client';

import { useState } from 'react';
import { BOT_CONFIGS, type BotConfig } from '@/lib/bot-config';

interface BotTabsProps {
  selectedBot: string;
  onSelectBot: (botId: string) => void;
}

export default function BotTabs({ selectedBot, onSelectBot }: BotTabsProps) {
  const [showInactive, setShowInactive] = useState(false);
  const allBots = Object.values(BOT_CONFIGS);
  const activeBots = allBots.filter(bot => bot.isActive);
  const inactiveBots = allBots.filter(bot => !bot.isActive);
  const displayedBots = showInactive ? allBots : activeBots;

  return (
    <div className="mb-8">
      <div className="flex items-center gap-4 flex-wrap">
        {/* Active Bots */}
        <div className="flex gap-2 flex-wrap">
          {displayedBots.map((bot) => (
            <button
              key={bot.id}
              onClick={() => onSelectBot(bot.id)}
              className={`
                px-4 py-2 rounded-lg font-medium text-sm transition-all
                ${
                  selectedBot === bot.id
                    ? 'bg-primary text-primary-foreground shadow-md'
                    : bot.isActive
                    ? 'bg-muted hover:bg-muted/80 text-foreground'
                    : 'bg-muted/50 text-muted-foreground opacity-60'
                }
              `}
            >
              {bot.name}
              {!bot.isActive && (
                <span className="ml-1 text-xs opacity-60">●</span>
              )}
            </button>
          ))}
        </div>

        {/* Toggle Inactive Bots */}
        {inactiveBots.length > 0 && (
          <button
            onClick={() => setShowInactive(!showInactive)}
            className="px-3 py-1.5 rounded-md text-xs font-medium bg-background border border-border text-muted-foreground hover:text-foreground transition-colors"
          >
            {showInactive ? '− Hide Inactive' : `+ ${inactiveBots.length} Inactive`}
          </button>
        )}
      </div>

      {/* Bot Info */}
      {selectedBot && BOT_CONFIGS[selectedBot] && (
        <div className="mt-4 text-sm text-muted-foreground">
          {BOT_CONFIGS[selectedBot].description}
          {!BOT_CONFIGS[selectedBot].isActive && (
            <span className="ml-2 text-xs bg-muted px-2 py-0.5 rounded">Inactive</span>
          )}
        </div>
      )}
    </div>
  );
}
