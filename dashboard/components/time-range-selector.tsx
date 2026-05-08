'use client'

export type TimeRange = '1W' | '1M' | '3M' | '6M' | '1Y' | 'ALL' | 'CUSTOM'

interface TimeRangeSelectorProps {
  selected: TimeRange
  onSelect: (range: TimeRange) => void
  onCustomClick?: () => void
  customLabel?: string
}

export const TIME_RANGES: { value: TimeRange; label: string; days: number | null }[] = [
  { value: '1W', label: '1W', days: 7 },
  { value: '1M', label: '1M', days: 30 },
  { value: '3M', label: '3M', days: 90 },
  { value: '6M', label: '6M', days: 180 },
  { value: '1Y', label: '1Y', days: 365 },
  { value: 'ALL', label: 'ALL', days: null },
  { value: 'CUSTOM', label: 'Custom', days: null },
]

export default function TimeRangeSelector({
  selected,
  onSelect,
  onCustomClick,
  customLabel
}: TimeRangeSelectorProps) {
  const handleClick = (value: TimeRange) => {
    if (value === 'CUSTOM' && onCustomClick) {
      onCustomClick()
    } else {
      onSelect(value)
    }
  }

  return (
    <div className="inline-flex flex-wrap rounded-lg border border-border bg-muted/50 p-1">
      {TIME_RANGES.map(({ value, label }) => (
        <button
          key={value}
          onClick={() => handleClick(value)}
          className={`
            px-3 py-1.5 text-sm font-medium rounded-md transition-all whitespace-nowrap
            ${selected === value
              ? 'bg-primary text-primary-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground'
            }
          `}
        >
          {value === 'CUSTOM' && customLabel ? customLabel : label}
        </button>
      ))}
    </div>
  )
}
