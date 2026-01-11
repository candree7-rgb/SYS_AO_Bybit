'use client';

import { useEffect, useState } from 'react';

interface TimeframeSelectorProps {
  selectedTimeframe: string;
  onSelectTimeframe: (timeframe: string) => void;
  botId?: string;
}

export default function TimeframeSelector({
  selectedTimeframe,
  onSelectTimeframe,
  botId
}: TimeframeSelectorProps) {
  const [availableTimeframes, setAvailableTimeframes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchTimeframes() {
      try {
        setLoading(true);
        const params = new URLSearchParams();
        if (botId) params.append('botId', botId);

        const response = await fetch(`/api/timeframes?${params}`);
        if (response.ok) {
          const timeframes = await response.json();
          setAvailableTimeframes(timeframes);
        }
      } catch (error) {
        console.error('Failed to fetch timeframes:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchTimeframes();
  }, [botId]);

  if (loading) {
    return (
      <div className="flex gap-2">
        <div className="h-8 w-16 bg-muted animate-pulse rounded-lg"></div>
        <div className="h-8 w-16 bg-muted animate-pulse rounded-lg"></div>
        <div className="h-8 w-16 bg-muted animate-pulse rounded-lg"></div>
      </div>
    );
  }

  if (availableTimeframes.length === 0) {
    return null;
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-sm text-muted-foreground font-medium">Timeframe:</span>

      {/* All Button */}
      <button
        onClick={() => onSelectTimeframe('all')}
        className={`
          px-3 py-1.5 rounded-md font-medium text-xs transition-all
          ${
            selectedTimeframe === 'all'
              ? 'bg-primary text-primary-foreground shadow-md'
              : 'bg-muted hover:bg-muted/80 text-foreground'
          }
        `}
      >
        All
      </button>

      {/* Timeframe Buttons */}
      {availableTimeframes.map((timeframe) => (
        <button
          key={timeframe}
          onClick={() => onSelectTimeframe(timeframe)}
          className={`
            px-3 py-1.5 rounded-md font-medium text-xs transition-all
            ${
              selectedTimeframe === timeframe
                ? 'bg-primary text-primary-foreground shadow-md'
                : 'bg-muted hover:bg-muted/80 text-foreground'
            }
          `}
        >
          {timeframe}
        </button>
      ))}
    </div>
  );
}
