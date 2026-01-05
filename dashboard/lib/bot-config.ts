export interface BotConfig {
  id: string;
  name: string;
  description: string;
  tpCount: number;  // Number of TP levels (1-5)
  dcaCount: number; // Number of DCA levels (0-2)
  hasTrailing: boolean;
  hasBreakeven: boolean;
  isActive: boolean; // Whether the bot is currently active
}

export const BOT_CONFIGS: Record<string, BotConfig> = {
  all: {
    id: 'all',
    name: 'All Bots',
    description: 'Combined performance',
    tpCount: 6,  // Max for display purposes (RVN has 6)
    dcaCount: 2, // Max for display purposes
    hasTrailing: true,
    hasBreakeven: true,
    isActive: true,
  },
  ao: {
    id: 'ao',
    name: 'AO',
    description: '1.5% equity, 3 TPs +Trailing, 2 DCAs',
    tpCount: 3,
    dcaCount: 2,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: true,
  },
  hsb: {
    id: 'hsb',
    name: 'HSB',
    description: '10% equity, 3 TPs, 1 DCA',
    tpCount: 3,
    dcaCount: 1,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: false,
  },
  rya: {
    id: 'rya',
    name: 'RYA',
    description: '5% equity, 3-5 TPs, Follow TP',
    tpCount: 5,
    dcaCount: 0,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: true,
  },
  rvn: {
    id: 'rvn',
    name: 'RVN',
    description: '5% equity, Low RR, Entry Zone, 6 TPs',
    tpCount: 6,
    dcaCount: 0,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: true,
  },
  fox: {
    id: 'fox',
    name: 'Fox',
    description: '5 TPs, no DCA, BE after TP1',
    tpCount: 5,
    dcaCount: 0,
    hasTrailing: false,
    hasBreakeven: true,
    isActive: false,
  },
};

export function getBotConfig(botId: string): BotConfig {
  return BOT_CONFIGS[botId] || BOT_CONFIGS.ao;
}

export function getAllBotIds(): string[] {
  return Object.keys(BOT_CONFIGS);
}

export function getActiveBotIds(): string[] {
  return Object.values(BOT_CONFIGS)
    .filter(bot => bot.isActive && bot.id !== 'all')
    .map(bot => bot.id);
}
