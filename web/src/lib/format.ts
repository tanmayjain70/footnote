/*
 * Everything here is en-GB. The client is a Manchester firm, the rents are
 * in pounds, and "04/01/2024" must never be read as the fourth of January by
 * one person and the first of April by another -- that is the kind of mistake
 * this product exists to stop, so dates are always spelled with the month.
 *
 * The exception is model spend, which the provider bills in US dollars and the
 * API stores as such. It gets its own formatter rather than being dressed up
 * as pounds.
 */
const FORMATTERS = new Map<string, Intl.NumberFormat>()

function formatter(currency: string, round: boolean): Intl.NumberFormat {
  const key = `${currency}:${round}`
  let found = FORMATTERS.get(key)
  if (!found) {
    found = new Intl.NumberFormat('en-GB', {
      style: 'currency',
      currency,
      maximumFractionDigits: round ? 0 : 2,
    })
    FORMATTERS.set(key, found)
  }
  return found
}

const COUNT = new Intl.NumberFormat('en-GB')

const COST = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
})

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = typeof value === 'string' ? Number(value) : value
  return Number.isNaN(n) ? null : n
}

/** `£42,500` for a rent; pass `round` for a figure shown next to many others. */
export function money(
  value: string | number | null | undefined,
  currency: string | null = 'GBP',
  round = false,
): string {
  const n = toNumber(value)
  if (n === null) return '—'
  return formatter(currency || 'GBP', round).format(n)
}

/** Model spend: `$0.0132`. Four decimals because a single answer costs a fraction of a cent. */
export function cost(value: string | number | null | undefined): string {
  const n = toNumber(value)
  if (n === null) return '—'
  return COST.format(n)
}

export function count(value: number | string | null | undefined): string {
  const n = toNumber(value)
  if (n === null) return '—'
  return COUNT.format(n)
}

/**
 * A date-only string (`2029-03-31`) is a calendar date, not an instant, so it
 * is built in local time. `new Date('2029-03-31')` would be UTC midnight and
 * show as the 30th to anyone west of Greenwich.
 */
function parseDate(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (match) return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  return new Date(value)
}

/** `31 Mar 2029`. */
export function shortDate(value: string | null | undefined): string {
  if (!value) return '—'
  return parseDate(value).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

/** `31 Mar, 14:05`. */
export function dateTime(value: string | null | undefined): string {
  if (!value) return '—'
  return parseDate(value).toLocaleString('en-GB', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * "4m ago". Takes either a number of seconds or an ISO timestamp, because
 * the health endpoint reports ages and everything else reports instants.
 */
export function since(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'never'
  const seconds =
    typeof value === 'number'
      ? value
      : Math.max(0, Math.floor((Date.now() - parseDate(value).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86_400)}d ago`
}

export function daysAgo(value: string | null | undefined): string {
  if (!value) return '—'
  const days = Math.floor((Date.now() - parseDate(value).getTime()) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

export function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10)
}

/** `should_have_refused` -> `Should have refused`. Codes are stored as they arrive. */
export function humanise(code: string | null | undefined): string {
  if (!code) return '—'
  const text = code.replace(/[_-]+/g, ' ').trim()
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** A fraction in 0..1 as `83%`. Eval rates arrive as fractions. */
export function percent(value: number | string | null | undefined, digits = 0): string {
  const n = toNumber(value)
  if (n === null) return '—'
  return `${(n * 100).toFixed(digits)}%`
}

/** `820 ms` below a second, `2.3 s` above it. */
export function latency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(1)} s`
}
