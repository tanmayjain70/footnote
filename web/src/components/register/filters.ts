/*
 * The register's filters are also the export's filters. The CSV is what ends
 * up in front of a landlord, so it has to be the table the administrator was
 * looking at, filter for filter; one function builds the query string for
 * both requests and nothing can drift between them.
 */
import { query } from '../../lib/api'
import type { RegisterRow } from '../../lib/types'

export interface RegisterFilters {
  portfolio: string
  /** ISO date; leases whose term ends before this day. */
  expiringBefore: string
  hasBreak: boolean
  includeUnreviewed: boolean
}

export const DEFAULT_FILTERS: RegisterFilters = {
  portfolio: '',
  expiringBefore: '',
  hasBreak: false,
  includeUnreviewed: false,
}

export function registerQuery(filters: RegisterFilters): string {
  return query({
    portfolio_id: filters.portfolio,
    expiring_before: filters.expiringBefore,
    // `false` would be sent as a filter for "no break", which nobody asks for.
    has_break: filters.hasBreak ? true : undefined,
    include_unreviewed: filters.includeUnreviewed ? true : undefined,
  })
}

/** The columns that carry an extracted value, in the order the table shows them. */
export const VALUE_KEYS = [
  'tenant_name',
  'unit',
  'property_address',
  'term_start',
  'term_end',
  'annual_rent_gbp',
  'rent_review_basis',
  'rent_review_date',
  'break_date',
  'break_notice_months',
  'repairing_obligation',
] as const

export type ValueKey = (typeof VALUE_KEYS)[number]

/**
 * Soonest expiry first. Leases with no confirmed end date go to the bottom:
 * the register exists to show what is coming up, and a blank is not "never",
 * it is "nobody has confirmed it yet".
 */
export function byTermEnd(a: RegisterRow, b: RegisterRow): number {
  if (a.term_end && b.term_end && a.term_end !== b.term_end) {
    return a.term_end < b.term_end ? -1 : 1
  }
  if (Boolean(a.term_end) !== Boolean(b.term_end)) return a.term_end ? -1 : 1
  return a.title.localeCompare(b.title)
}

/**
 * Which of a row's shown values nobody has confirmed. The API fills unreviewed
 * values in only when asked and the row does not say which ones they are, so
 * the reviewed view of the same document is the reference: a value present
 * here and blank there is one a person has not yet agreed to.
 */
export function unreviewedKeys(
  row: RegisterRow,
  reviewed: RegisterRow | undefined,
): Set<ValueKey> {
  const keys = new Set<ValueKey>()
  if (!reviewed) return keys
  for (const key of VALUE_KEYS) {
    if (row[key] !== null && reviewed[key] === null) keys.add(key)
  }
  return keys
}
