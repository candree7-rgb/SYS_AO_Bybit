'use client';

import { useState } from 'react';

interface DateRangePickerProps {
  isOpen: boolean;
  onClose: () => void;
  onApply: (from: string, to: string) => void;
}

export default function DateRangePicker({ isOpen, onClose, onApply }: DateRangePickerProps) {
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');

  if (!isOpen) return null;

  const handleApply = () => {
    if (fromDate && toDate) {
      onApply(fromDate, toDate);
      onClose();
    }
  };

  const handleReset = () => {
    setFromDate('');
    setToDate('');
  };

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-md">
        <div className="bg-card border border-border rounded-lg p-6 shadow-xl mx-4">
          <h3 className="text-lg font-bold mb-4">Custom Date Range</h3>

          <div className="space-y-4">
            {/* From Date */}
            <div>
              <label className="block text-sm font-medium text-muted-foreground mb-2">
                From
              </label>
              <input
                type="date"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
                className="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>

            {/* To Date */}
            <div>
              <label className="block text-sm font-medium text-muted-foreground mb-2">
                To
              </label>
              <input
                type="date"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
                className="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
          </div>

          {/* Actions */}
          <div className="flex gap-3 mt-6">
            <button
              onClick={handleReset}
              className="flex-1 px-4 py-2 border border-border rounded-md text-muted-foreground hover:bg-muted/50 transition-colors"
            >
              Reset
            </button>
            <button
              onClick={onClose}
              className="flex-1 px-4 py-2 border border-border rounded-md hover:bg-muted/50 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleApply}
              disabled={!fromDate || !toDate}
              className="flex-1 px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
