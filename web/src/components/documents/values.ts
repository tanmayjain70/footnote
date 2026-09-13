/*
 * Small helpers shared by the documents screens. Formatting an extracted value
 * depends on the field's type -- a date must come out as `31 Mar 2029`, never
 * as the ISO string the API stores, because the whole point of the register
 * is that a person reads it and decides whether to trust it.
 */
import { count, humanise, money, shortDate } from '../../lib/format'
import type { DocumentType, ExtractedJson, FieldType, ValueIssue } from '../../lib/types'

/** Statuses a background job is still working through; screens poll while one is showing. */
const LIVE = new Set(['queued', 'processing', 'running'])

export function isLive(status: string | null | undefined): boolean {
  return Boolean(status && LIVE.has(status))
}

export const DOC_TYPES: { value: DocumentType; label: string }[] = [
  { value: 'lease', label: 'Lease' },
  { value: 'deed_of_variation', label: 'Deed of variation' },
  { value: 'side_letter', label: 'Side letter' },
  { value: 'notice', label: 'Notice' },
  { value: 'other', label: 'Other' },
]

/** The typed value as a person would read it; `—` when there is none. */
export function formatValue(type: FieldType, value: ExtractedJson | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  switch (type) {
    case 'date':
      return typeof value === 'string' ? shortDate(value) : String(value)
    case 'money':
      return typeof value === 'boolean' ? String(value) : money(value)
    case 'integer':
      return typeof value === 'boolean' ? String(value) : count(value)
    case 'bool':
      if (value === true) return 'Yes'
      if (value === false) return 'No'
      return String(value)
    case 'enum':
      return humanise(String(value))
    default:
      return String(value)
  }
}

/**
 * The value as text for the correction input, in the form `parse_value` on the
 * API accepts for that type: ISO for dates, `true`/`false` for booleans, the
 * raw code for enums.
 */
export function editableValue(value: ExtractedJson | undefined): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

/**
 * Why a value should be looked at rather than trusted. These are the checks
 * the API runs on what the model returned; the label is what a lease
 * administrator sees, the title says what went wrong.
 */
export const ISSUES: Record<ValueIssue, { label: string; title: string }> = {
  no_evidence: {
    label: 'No evidence',
    title: 'The model found nothing for this term in the document.',
  },
  invalid_chunk: {
    label: 'Wrong passage',
    title:
      'The model pointed at a passage that is not in this document, so the reference was dropped.',
  },
  quote_not_found: {
    label: 'Quote not found',
    title:
      'The quote does not appear in the passage the model pointed at. Check it against the page before confirming.',
  },
  unparseable: {
    label: 'Could not read',
    title: 'The value as written could not be read as this type of term. Correct it by hand.',
  },
}

/** `1.2 MB`, `640 KB`. */
export function fileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1_048_576) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1_048_576).toFixed(1)} MB`
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * A pattern that finds `needle` in page text regardless of case and of how
 * whitespace fell: the quote came from a chunk whose text may have been
 * re-flowed, so a line break in the page must still match a space in the quote.
 */
export function highlightPattern(needle: string | null | undefined): RegExp | null {
  const words = (needle ?? '').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return null
  return new RegExp(words.map(escapeRegExp).join('\\s+'), 'i')
}

/** The character range of the first match, in the page's own text. */
export function findRange(text: string, pattern: RegExp | null): [number, number] | null {
  if (!pattern) return null
  const match = pattern.exec(text)
  return match ? [match.index, match.index + match[0].length] : null
}
