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
    tpCount: 3,  // Max TPs for active bots (ao, zia)
    dcaCount: 3, // Max DCAs for active bots (ao, zia)
    hasTrailing: true,
    hasBreakeven: true,
    isActive: true,
  },
  ao: {
    id: 'ao',
    name: 'AO',
    description: '1.5% equity, 3 TPs +Trailing, 3 DCAs',
    tpCount: 3,
    dcaCount: 3,
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
    isActive: false,
  },
  rvn: {
    id: 'rvn',
    name: 'RVN',
    description: '5% equity, Low RR, Entry Zone, 6 TPs',
    tpCount: 6,
    dcaCount: 0,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: false,
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
  zeii: {
    id: 'zeii',
    name: 'ZEI',
    description: '2% equity, Dynamic leverage based on SL distance',
    tpCount: 3,
    dcaCount: 2,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: false,
  },
  aoalgo: {
    id: 'aoalgo',
    name: 'AO Algo',
    description: 'Algorithmic AO bot',
    tpCount: 3,
    dcaCount: 2,
    hasTrailing: true,
    hasBreakeven: true,
    isActive: false,
  },
  zia: {
    id: 'zia',
    name: 'ZIA',
    description: 'Similar to AO: 3 TPs, 3 DCAs, no trailing',
    tpCount: 3,
    dcaCount: 3,
    hasTrailing: false,
    hasBreakeven: true,
    isActive: true,
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
