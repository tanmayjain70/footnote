import { Fragment } from 'react'
import { dateTime } from '../../lib/format'
import type { EvalRun } from '../../lib/types'
import { StatusPill, Table, Td, Th, cx } from '../ui'
import { METRICS, MODE_LABELS, formatMetric } from './metrics'
import { RunDetail } from './RunDetail'

/*
 * Runs newest first, every metric side by side, so a change in retrieval
 * reads as a change in a column rather than a feeling. A row opens to its
 * per-question results; the numbers in the row are the numbers to compare,
 * the detail is where to look when one of them moved.
 */
export function RunsTable({
  runs,
  expandedId,
  onToggle,
}: {
  runs: EvalRun[]
  expandedId: string | null
  onToggle: (id: string) => void
}) {
  const columns = 3 + METRICS.length

  return (
    <Table>
      <thead>
        <tr>
          <Th>Mode</Th>
          <Th>Started</Th>
          <Th>Status</Th>
          {METRICS.map((metric) => (
            <Th key={metric.key} right>
              <span title={metric.explain}>{metric.label}</span>
            </Th>
          ))}
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const open = run.id === expandedId
          return (
            <Fragment key={run.id}>
              <tr
                onClick={() => onToggle(run.id)}
                aria-expanded={open}
                className={cx('cursor-pointer', open ? 'bg-brand-50/40' : 'hover:bg-slate-50')}
              >
                <Td className="whitespace-nowrap font-medium text-slate-900">
                  <span className="mr-1.5 inline-block w-3 text-slate-400" aria-hidden>
                    {open ? '▾' : '▸'}
                  </span>
                  {MODE_LABELS[run.mode]}
                </Td>
                <Td className="whitespace-nowrap text-slate-600">
                  {dateTime(run.started_at)}
                  {run.started_by_name && (
                    <span className="block text-xs text-slate-400">{run.started_by_name}</span>
                  )}
                </Td>
                <Td>
                  <StatusPill status={run.status} title={run.error ?? undefined} />
                </Td>
                {METRICS.map((metric) => {
                  const text = formatMetric(metric, run.totals)
                  // A retrieval run has no answer metrics; the cell says so
                  // on hover rather than looking like a missing number.
                  const notForMode = Boolean(metric.mode && metric.mode !== run.mode)
                  const raw = run.totals[metric.key]
                  const alarming = metric.inverse && typeof raw === 'number' && raw > 0
                  return (
                    <Td
                      key={metric.key}
                      right
                      className={cx(
                        'whitespace-nowrap',
                        text === '—' || notForMode ? 'text-slate-400' : alarming ? 'font-medium text-bad-700' : 'text-slate-800',
                      )}
                    >
                      <span title={notForMode ? 'End-to-end runs only' : undefined}>{text}</span>
                    </Td>
                  )
                })}
              </tr>
              {open && (
                <tr>
                  <td colSpan={columns} className="border-b border-slate-100 bg-slate-50 px-5 py-4">
                    <RunDetail runId={run.id} />
                  </td>
                </tr>
              )}
            </Fragment>
          )
        })}
      </tbody>
    </Table>
  )
}
