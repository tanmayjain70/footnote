import { Link } from 'react-router-dom'
import { count, humanise, money, shortDate } from '../../lib/format'
import type { RegisterReview, RegisterRow } from '../../lib/types'
import { Badge, Table, Td, Th, cx } from '../ui'
import { unreviewedKeys } from './filters'
import type { ValueKey } from './filters'

/*
 * The wide table. Twelve columns is more than fits on a laptop, and that is
 * accepted: this is a spreadsheet, it scrolls sideways inside its card, and
 * the tenant column stays readable at the left because it is what a reader
 * scans down before reading across.
 */

type Kind = 'text' | 'date' | 'money' | 'integer' | 'enum'

interface Column {
  key: ValueKey
  label: string
  kind: Kind
  right?: boolean
}

const COLUMNS: Column[] = [
  { key: 'unit', label: 'Unit', kind: 'text' },
  { key: 'property_address', label: 'Property', kind: 'text' },
  { key: 'term_start', label: 'Term start', kind: 'date' },
  { key: 'term_end', label: 'Term end', kind: 'date' },
  { key: 'annual_rent_gbp', label: 'Annual rent', kind: 'money', right: true },
  { key: 'rent_review_basis', label: 'Review basis', kind: 'enum' },
  { key: 'rent_review_date', label: 'Review date', kind: 'date' },
  { key: 'break_date', label: 'Break date', kind: 'date' },
  { key: 'break_notice_months', label: 'Break notice', kind: 'integer', right: true },
  { key: 'repairing_obligation', label: 'Repairs', kind: 'enum' },
]

function formatCell(kind: Kind, value: string | number | null): string {
  if (value === null || value === '') return '—'
  switch (kind) {
    case 'date':
      return shortDate(String(value))
    case 'money':
      return money(value)
    case 'integer':
      return typeof value === 'number' ? `${count(value)} month${value === 1 ? '' : 's'}` : String(value)
    case 'enum':
      return humanise(String(value))
    default:
      return String(value)
  }
}

/**
 * A value cell. An unreviewed value is muted and carries a badge rather than
 * being hidden or coloured in: the reader asked to see it, and must still be
 * able to tell it apart from one a person has signed off.
 */
function ValueCell({
  column,
  value,
  unreviewed,
}: {
  column: Column
  value: string | number | null
  unreviewed: boolean
}) {
  const text = formatCell(column.kind, value)
  const blank = text === '—'
  return (
    <Td
      right={column.right}
      className={cx(
        'whitespace-nowrap',
        column.kind !== 'text' && 'tabular',
        blank ? 'text-slate-400' : unreviewed ? 'text-slate-500' : 'text-slate-800',
      )}
    >
      {text}
      {unreviewed && !blank && (
        <span className="ml-1.5 align-middle">
          <Badge tone="amber" title="Extracted by the model; nobody has confirmed it yet">
            unreviewed
          </Badge>
        </span>
      )}
    </Td>
  )
}

const SEGMENTS: { key: keyof RegisterReview; label: string; colour: string }[] = [
  { key: 'confirmed', label: 'confirmed', colour: 'bg-good-600' },
  { key: 'corrected', label: 'corrected', colour: 'bg-brand-500' },
  { key: 'pending', label: 'pending', colour: 'bg-note-600' },
  { key: 'rejected', label: 'rejected', colour: 'bg-slate-300' },
]

/**
 * How far the review of one document has got. The bar is the four counts in
 * proportion; the text underneath is the same counts in words, because a bar
 * with no numbers is decoration.
 */
function ReviewProgress({ row }: { row: RegisterRow }) {
  const total = SEGMENTS.reduce((sum, segment) => sum + row.review[segment.key], 0)
  const parts = SEGMENTS.filter((segment) => row.review[segment.key] > 0)
  return (
    <div className="min-w-44">
      <div className="flex items-center gap-2">
        <div
          className="flex h-1.5 w-24 overflow-hidden rounded-full bg-slate-100"
          role="img"
          aria-label={parts.map((s) => `${row.review[s.key]} ${s.label}`).join(', ') || 'no values'}
        >
          {total > 0 &&
            parts.map((segment) => (
              <span
                key={segment.key}
                className={segment.colour}
                style={{ width: `${(row.review[segment.key] / total) * 100}%` }}
              />
            ))}
        </div>
        {row.complete ? (
          <Badge tone="green" title="Every value the model found has been reviewed">
            complete
          </Badge>
        ) : row.review.pending > 0 ? (
          <Badge tone="amber">{count(row.review.pending)} pending</Badge>
        ) : null}
      </div>
      <p className="tabular mt-1 text-xs text-slate-500">
        {parts.length
          ? parts.map((segment) => `${count(row.review[segment.key])} ${segment.label}`).join(' · ')
          : 'No values extracted'}
        {' · '}
        <Link to={`/documents/${row.document_id}`} className="font-medium text-brand-700 hover:underline">
          {row.review.pending > 0 ? 'Review →' : 'Open →'}
        </Link>
      </p>
    </div>
  )
}

export function RegisterTable({
  rows,
  reviewed,
  showUnreviewed,
}: {
  rows: RegisterRow[]
  /** The same documents with only reviewed values, keyed by document id; used to mark the rest. */
  reviewed: Map<string, RegisterRow> | undefined
  showUnreviewed: boolean
}) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Tenant</Th>
          {COLUMNS.map((column) => (
            <Th key={column.key} right={column.right}>
              {column.label}
            </Th>
          ))}
          <Th>Review</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const marks = showUnreviewed ? unreviewedKeys(row, reviewed?.get(row.document_id)) : null
          const tenantUnreviewed = Boolean(marks?.has('tenant_name'))
          return (
            <tr key={row.document_id} className="align-top hover:bg-slate-50">
              <Td className="min-w-52 max-w-xs">
                <span
                  className={cx(
                    'block font-medium',
                    row.tenant_name ? (tenantUnreviewed ? 'text-slate-500' : 'text-slate-900') : 'text-slate-400',
                  )}
                >
                  {row.tenant_name ?? '—'}
                  {tenantUnreviewed && (
                    <span className="ml-1.5 align-middle">
                      <Badge tone="amber" title="Extracted by the model; nobody has confirmed it yet">
                        unreviewed
                      </Badge>
                    </span>
                  )}
                </span>
                <Link
                  to={`/documents/${row.document_id}`}
                  className="mt-0.5 block truncate text-xs text-slate-500 hover:text-brand-700"
                  title={`${row.title} · ${row.portfolio}`}
                >
                  {row.title} · {row.portfolio}
                </Link>
              </Td>
              {COLUMNS.map((column) => (
                <ValueCell
                  key={column.key}
                  column={column}
                  value={row[column.key]}
                  unreviewed={Boolean(marks?.has(column.key))}
                />
              ))}
              <Td>
                <ReviewProgress row={row} />
              </Td>
            </tr>
          )
        })}
      </tbody>
    </Table>
  )
}
