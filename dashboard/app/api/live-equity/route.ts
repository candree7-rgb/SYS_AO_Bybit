import { NextResponse } from 'next/server';
import crypto from 'crypto';

// Bybit API credentials from environment
const BYBIT_API_KEY = process.env.BYBIT_API_KEY || '';
const BYBIT_API_SECRET = process.env.BYBIT_API_SECRET || '';
const BYBIT_TESTNET = process.env.BYBIT_TESTNET === 'true';
const BYBIT_DEMO = process.env.BYBIT_DEMO === 'true';
const ACCOUNT_TYPE = process.env.ACCOUNT_TYPE || 'UNIFIED';

function getBybitBaseUrl(): string {
  if (BYBIT_DEMO) return 'https://api-demo.bybit.com';
  if (BYBIT_TESTNET) return 'https://api-testnet.bybit.com';
  return 'https://api.bybit.com';
}

function generateSignature(timestamp: string, apiKey: string, recvWindow: string, queryString: string): string {
  const message = timestamp + apiKey + recvWindow + queryString;
  return crypto.createHmac('sha256', BYBIT_API_SECRET).update(message).digest('hex');
}

export async function GET() {
  try {
    if (!BYBIT_API_KEY || !BYBIT_API_SECRET) {
      throw new Error('Bybit credentials not configured');
    }

    const timestamp = Date.now().toString();
    const recvWindow = '5000';
    const queryString = `accountType=${ACCOUNT_TYPE}`;
    const signature = generateSignature(timestamp, BYBIT_API_KEY, recvWindow, queryString);

    const baseUrl = getBybitBaseUrl();
    const url = `${baseUrl}/v5/account/wallet-balance?${queryString}`;

    const response = await fetch(url, {
      headers: {
        'X-BAPI-API-KEY': BYBIT_API_KEY,
        'X-BAPI-SIGN': signature,
        'X-BAPI-SIGN-TYPE': '2',
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-RECV-WINDOW': recvWindow,
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      throw new Error(`Bybit API returned ${response.status}`);
    }

    const data = await response.json();

    if (data.retCode !== 0) {
      throw new Error(`Bybit error: ${data.retMsg}`);
    }

    const list = data.result?.list || [];
    if (list.length === 0) {
      throw new Error('No wallet balance data');
    }

    const equity = parseFloat(list[0].totalEquity || list[0].totalWalletBalance || list[0].totalAvailableBalance || '0');

    return NextResponse.json({
      equity,
      timestamp: new Date().toISOString(),
    });
  } catch (error) {
    console.error('Failed to fetch live equity from Bybit:', error);

    // Fallback: Get latest equity from database
    try {
      const { getDailyEquity } = await import('@/lib/db');
      const dailyEquity = await getDailyEquity(1);

      if (dailyEquity.length > 0) {
        return NextResponse.json({
          equity: parseFloat(dailyEquity[0].equity.toString()),
          timestamp: dailyEquity[0].date,
          fallback: true,
        });
      }
    } catch (dbError) {
      console.error('Failed to fetch from database:', dbError);
    }

    return NextResponse.json(
      { error: 'Failed to fetch equity', equity: 0 },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';
export const revalidate = 0;

