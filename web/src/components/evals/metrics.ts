/*
 * What each number on a run means, in one line, because "MRR 0.71" is only
 * useful to somebody who already knows what MRR is. The same list drives the
 * table headings and the explanation above the table, so they cannot drift.
 */
import { cost, percent } from '../../lib/format'
import type { EvalMode, EvalTotals } from '../../lib/types'

export interface Metric {
  key: keyof EvalTotals
  label: string
  explain: string
  kind: 'rate' | 'rank' | 'cost'
  /** Set when only one mode produces the number. */
  mode?: EvalMode
  /** Lower is better; the table renders a non-zero value in red rather than green. */
  inverse?: boolean
}

export const METRICS: Metric[] = [
  {
    key: 'doc_hit_rate',
    label: 'Doc hit',
    kind: 'rate',
    explain: 'The expected document was among the passages retrieved for the question.',
  },
  {
    key: 'page_hit_rate',
    label: 'Page hit',
    kind: 'rate',
    explain: 'A retrieved passage came from one of the expected pages of that document.',
  },
  {
    key: 'mrr',
    label: 'MRR',
    kind: 'rank',
    explain:
      'Mean reciprocal rank of the first hit: 1.00 means the right passage was always ranked first, 0.50 that it was second on average.',
  },
  {
    key: 'answer_rate',
    label: 'Answer rate',
    kind: 'rate',
    mode: 'end_to_end',
    explain: 'Answerable questions that got an answer with at least one verified citation.',
  },
  {
    key: 'correct_refusal_rate',
    label: 'Correct refusals',
    kind: 'rate',
    mode: 'end_to_end',
    explain: 'Unanswerable questions the system declined to answer, as it should.',
  },
  {
    key: 'false_answer_rate',
    label: 'False answers',
    kind: 'rate',
    mode: 'end_to_end',
    inverse: true,
    explain:
      'Answerable questions that cited the wrong document, plus unanswerable ones that were answered anyway. This is the number to drive to zero.',
  },
  {
    key: 'cost_usd',
    label: 'Cost',
    kind: 'cost',
    explain:
      'Model spend for the run. Retrieval runs cost nothing; an end-to-end run asks the model every question.',
  },
]

export function formatMetric(metric: Metric, totals: EvalTotals): string {
  const value = totals[metric.key]
  if (value === null || value === undefined) return '—'
  switch (metric.kind) {
    case 'rate':
      return percent(value)
    case 'rank':
      return typeof value === 'number' ? value.toFixed(2) : String(value)
    case 'cost':
      return cost(typeof value === 'number' || typeof value === 'string' ? value : null)
  }
}

export const MODE_LABELS: Record<EvalMode, string> = {
  retrieval: 'Retrieval',
  end_to_end: 'End to end',
}

/** `3, 4` or `3 4` to `[3, 4]`; null when anything in it is not a page number. */
export function parsePages(text: string): number[] | null {
  const parts = text.split(/[\s,;]+/).filter(Boolean)
  const pages: number[] = []
  for (const part of parts) {
    if (!/^\d+$/.test(part)) return null
    const n = Number(part)
    if (n < 1) return null
    pages.push(n)
  }
  return [...new Set(pages)].sort((a, b) => a - b)
}
