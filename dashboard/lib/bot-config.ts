export interface BotConfig {
  id: string;
  name: string;
  description: string;
  tpCount: number;  // Number of TP levels (1-5)
  dcaCount: number; // Number of DCA levels (0-2)
  hasTrailing: boolean;
  hasBreakeven: boolean;
}

export const BOT_CONFIGS: Record<string, BotConfig> = {
  ao: {
    id: 'ao',
    name: 'AO Bot',
    description: 'Original strategy with 3 TPs and 2 DCAs',
    tpCount: 3,
    dcaCount: 2,
    hasTrailing: true,
    hasBreakeven: true,
  },
  fox: {
    id: 'fox',
    name: 'Fox Bot',
    description: '5 TPs, no DCA, breakeven after TP1',
    tpCount: 5,
    dcaCount: 0,
    hasTrailing: false,
    hasBreakeven: true,
  },
};

export function getBotConfig(botId: string): BotConfig {
  return BOT_CONFIGS[botId] || BOT_CONFIGS.ao;
}

export function getAllBotIds(): string[] {
  return Object.keys(BOT_CONFIGS);
}
