import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../../lib/api'
import { count, humanise, shortDate } from '../../lib/format'
import type { EvalQuestion, MePortfolio } from '../../lib/types'
import { Badge, Card, Empty, ErrorNote, Notice, Pager, Select, Spinner, Table, Td, Th, cx } from '../ui'

const LIMIT = 25

export const QUESTIONS_KEY = ['evals', 'questions'] as const

export interface GoldenSummary {
  active: number
  answerable: number
  unanswerable: number
  inactive: number
}

/** The counts the Stat tiles show. Only active questions take part in a run, so only they are counted. */
export function summarise(questions: EvalQuestion[]): GoldenSummary {
  let active = 0
  let answerable = 0
  for (const question of questions) {
    if (!question.active) continue
    active += 1
    if (question.answerable) answerable += 1
  }
  return {
    active,
    answerable,
    unanswerable: active - answerable,
    inactive: questions.length - active,
  }
}

type Filter = 'all' | 'answerable' | 'unanswerable'

/*
 * The golden set itself. Deactivating a question keeps it -- with its
 * history in past runs -- but leaves it out of the next one, which is the
 * right thing for a question that turned out to be badly phrased: deleting
 * it would make old and new runs incomparable in a way nobody could see.
 */
export function GoldenSet({
  questions,
  portfolios,
  loading,
  error,
}: {
  questions: EvalQuestion[] | undefined
  portfolios: MePortfolio[]
  loading: boolean
  error: unknown
}) {
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<Filter>('all')
  const [offset, setOffset] = useState(0)
  const [toggleError, setToggleError] = useState<string | null>(null)

  const toggle = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      api.patch<EvalQuestion>(`/evals/questions/${id}`, { active }),
    onSuccess: (updated) => {
      setToggleError(null)
      queryClient.setQueryData<EvalQuestion[]>(QUESTIONS_KEY, (current) =>
        current?.map((q) => (q.id === updated.id ? updated : q)),
      )
    },
    onError: (err) =>
      setToggleError(err instanceof ApiError ? err.message : 'The change was not saved.'),
  })

  const portfolioNames = new Map(portfolios.map((p) => [p.id, p.name]))
  const all = questions ?? []
  const filtered = all.filter((q) =>
    filter === 'all' ? true : filter === 'answerable' ? q.answerable : !q.answerable,
  )
  const shown = filtered.slice(offset, offset + LIMIT)

  return (
    <Card
      title={`Golden set · ${count(all.length)} question${all.length === 1 ? '' : 's'}`}
      subtitle="Every run scores the active questions. Deactivate one to keep its history but leave it out."
      actions={
        <Select
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value as Filter)
            setOffset(0)
          }}
          aria-label="Filter questions"
        >
          <option value="all">All questions</option>
          <option value="answerable">Answerable</option>
          <option value="unanswerable">Unanswerable</option>
        </Select>
      }
    >
      {toggleError && <ErrorNote>{toggleError}</ErrorNote>}
      {loading ? (
        <Spinner label="Loading the golden set" />
      ) : error ? (
        <Notice tone="bad">The golden set could not be loaded.</Notice>
      ) : !shown.length ? (
        <Empty title={all.length ? 'No questions match' : 'No questions yet'}>
          {all.length
            ? 'Try the other filter.'
            : 'Add a question above, or seed the demo data, which generates a set from the leases.'}
        </Empty>
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>Question</Th>
                <Th>Expected</Th>
                <Th>Portfolio</Th>
                <Th>Source</Th>
                <Th>Added</Th>
                <Th right>Active</Th>
              </tr>
            </thead>
            <tbody>
              {shown.map((q) => (
                <tr key={q.id} className={cx('align-top', !q.active && 'text-slate-400')}>
                  <Td className="max-w-lg">
                    <span className={cx('block', q.active ? 'text-slate-900' : 'text-slate-500')}>
                      {q.question}
                    </span>
                    {q.notes && <span className="mt-0.5 block text-xs text-slate-500 italic">{q.notes}</span>}
                  </Td>
                  <Td className="min-w-48">
                    {q.answerable ? (
                      <>
                        <span className="block text-slate-800">
                          {q.expected_document_title ?? (
                            <span className="text-slate-400">Document no longer exists</span>
                          )}
                        </span>
                        {q.expected_pages.length > 0 && (
                          <span className="tabular block text-xs text-slate-500">
                            p. {q.expected_pages.join(', ')}
                          </span>
                        )}
                      </>
                    ) : (
                      <Badge tone="slate" title="The right answer is a refusal">
                        should refuse
                      </Badge>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap">
                    {q.portfolio_id ? (portfolioNames.get(q.portfolio_id) ?? 'Unknown') : 'Any'}
                  </Td>
                  <Td className="whitespace-nowrap">{humanise(q.source)}</Td>
                  <Td className="whitespace-nowrap">{shortDate(q.created_at)}</Td>
                  <Td right>
                    <input
                      type="checkbox"
                      checked={q.active}
                      disabled={toggle.isPending}
                      onChange={(e) => toggle.mutate({ id: q.id, active: e.target.checked })}
                      aria-label={q.active ? 'Deactivate this question' : 'Activate this question'}
                      className="h-4 w-4 rounded border-slate-300 accent-brand-600"
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pager total={filtered.length} limit={LIMIT} offset={offset} onChange={setOffset} />
        </>
      )}
    </Card>
  )
}
