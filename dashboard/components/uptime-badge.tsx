'use client'

const BOT_START = process.env.NEXT_PUBLIC_BOT_START_DATE || '2026-02-12'

function formatUptime(startDate: string): string {
  const start = new Date(startDate + 'T00:00:00Z')
  const now = new Date()
  const diffMs = now.getTime() - start.getTime()
  if (diffMs < 0) return 'starting soon'

  const totalDays = Math.floor(diffMs / (1000 * 60 * 60 * 24))
  const years = Math.floor(totalDays / 365)
  const remainingAfterYears = totalDays - years * 365
  const months = Math.floor(remainingAfterYears / 30)
  const remainingAfterMonths = remainingAfterYears - months * 30
  const weeks = Math.floor(remainingAfterMonths / 7)
  const days = remainingAfterMonths - weeks * 7

  // Progressive: show up to 2 meaningful units
  const parts: string[] = []
  if (years > 0) parts.push(`${years}y`)
  if (months > 0) parts.push(`${months}mo`)
  if (parts.length < 2 && weeks > 0) parts.push(`${weeks}w`)
  if (parts.length < 2 && days > 0) parts.push(`${days}d`)
  if (parts.length === 0) parts.push('today')

  return parts.join(' ')
}

export default function UptimeBadge() {
  const uptime = formatUptime(BOT_START)

  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400/90 bg-emerald-500/10 border border-emerald-500/20 rounded-full px-2.5 py-0.5">
      <span className="relative flex h-1.5 w-1.5">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-400" />
      </span>
      {uptime}
    </span>
  )
}
