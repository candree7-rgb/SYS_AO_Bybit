'use client'

import { useState } from 'react'

interface DateRangePickerProps {
  isOpen: boolean
  onClose: () => void
  onApply: (from: string, to: string) => void
}

export default function DateRangePicker({ isOpen, onClose, onApply }: DateRangePickerProps) {
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')

  if (!isOpen) return null

  const handleApply = () => {
    if (fromDate && toDate) {
      onApply(fromDate, toDate)
      onClose()
    }
  }

  const handleReset = () => {
    setFromDate('')
    setToDate('')
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="relative bg-card border border-border rounded-lg p-6 shadow-xl w-full max-w-sm mx-4">
        <h3 className="text-lg font-semibold mb-4">Custom Date Range</h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-muted-foreground mb-1">From</label>
            <input
              type="date"
              value={fromDate}
              onChange={(e) => setFromDate(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
          <div>
            <label className="block text-sm text-muted-foreground mb-1">To</label>
            <input
              type="date"
              value={toDate}
              onChange={(e) => setToDate(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
        </div>
        <div className="flex gap-2 mt-6">
          <button
            onClick={handleReset}
            className="flex-1 px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
          >
            Reset
          </button>
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 text-sm font-medium bg-muted hover:bg-muted/80 rounded-md transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleApply}
            disabled={!fromDate || !toDate}
            className="flex-1 px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-md hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  )
}
