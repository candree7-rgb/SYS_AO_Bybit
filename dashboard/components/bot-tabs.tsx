'use client';

import { BOT_CONFIGS, type BotConfig } from '@/lib/bot-config';

interface BotTabsProps {
  selectedBot: string;
  onSelectBot: (botId: string) => void;
}

export default function BotTabs({ selectedBot, onSelectBot }: BotTabsProps) {
  const bots = Object.values(BOT_CONFIGS);

  return (
    <div className="mb-8">
      <div className="border-b border-border">
        <nav className="flex space-x-8" aria-label="Tabs">
          {bots.map((bot) => (
            <button
              key={bot.id}
              onClick={() => onSelectBot(bot.id)}
              className={`
                py-4 px-1 border-b-2 font-medium text-sm transition-colors
                ${
                  selectedBot === bot.id
                    ? 'border-primary text-foreground'
                    : 'border-transparent text-muted-foreground hover:text-foreground hover:border-border'
                }
              `}
            >
              <div className="flex flex-col items-start">
                <span className="font-bold text-lg">{bot.name}</span>
                <span className="text-xs text-muted-foreground mt-1">
                  {bot.description}
                </span>
              </div>
            </button>
          ))}
        </nav>
      </div>
    </div>
  );
}
