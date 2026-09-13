import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, query } from '../../lib/api'
import { count, humanise, since } from '../../lib/format'
import type { Page, QuestionOut } from '../../lib/types'
import { Empty, Spinner, cx } from '../ui'

/*
 * The left column: what has been asked recently, newest first. Managers and
 * administrators see their own questions; the director sees everyone's, with
 * the asker's name, because "what are people asking, and is it being
 * answered" is a director's question.
 */

const LIMIT = 30

const DOTS: Record<string, string> = {
  answered: 'bg-good-600',
  unanswered: 'bg-note-600',
  failed: 'bg-bad-600',
  budget_exhausted: 'bg-bad-600',
}

export function QuestionHistory({
  currentId,
  enabled,
  viewerId,
}: {
  /** The question on screen, highlighted in the list. */
  currentId: string | null
  /** False for roles that cannot ask: the endpoint would refuse them. */
  enabled: boolean
  viewerId: string | undefined
}) {
  const questions = useQuery({
    queryKey: ['questions', { limit: LIMIT }],
    queryFn: () => api.get<Page<QuestionOut>>(`/questions${query({ limit: LIMIT })}`),
    enabled,
  })

  const items = questions.data?.items ?? []
  const total = questions.data?.total ?? 0

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 px-1">
        <h2 className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
          Recent questions
        </h2>
        {currentId && (
          <Link to="/" className="text-xs font-medium text-brand-700 hover:underline">
            New question
          </Link>
        )}
      </div>

      {!enabled ? (
        <p className="mt-2 px-1 text-xs text-slate-500">
          Your role reads the register and the documents. Asking is for managers and
          administrators, because each question spends the model budget.
        </p>
      ) : questions.isLoading ? (
        <Spinner label="Loading questions" />
      ) : items.length === 0 ? (
        <div className="mt-2">
          <Empty title="Nothing asked yet">Your questions will be listed here.</Empty>
        </div>
      ) : (
        <>
          <ul className="mt-2 space-y-0.5">
            {items.map((question) => {
              const current = question.id === currentId
              const someoneElse = question.user_id !== viewerId && question.user_name
              return (
                <li key={question.id}>
                  <Link
                    to={`/questions/${question.id}`}
                    aria-current={current ? 'page' : undefined}
                    className={cx(
                      'block rounded-lg px-3 py-2 transition',
                      current ? 'bg-brand-50' : 'hover:bg-slate-100',
                    )}
                  >
                    <p
                      className={cx(
                        'line-clamp-2 text-sm',
                        current ? 'font-medium text-brand-900' : 'text-slate-800',
                      )}
                    >
                      {question.text}
                    </p>
                    <p className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[11px] text-slate-500">
                      <span
                        className={cx(
                          'inline-block h-1.5 w-1.5 shrink-0 rounded-full',
                          DOTS[question.status] ?? 'bg-slate-400',
                        )}
                        aria-hidden
                      />
                      <span>{humanise(question.status)}</span>
                      <span aria-hidden>·</span>
                      <span className="shrink-0">{since(question.created_at)}</span>
                      {someoneElse && (
                        <>
                          <span aria-hidden>·</span>
                          <span className="truncate">{question.user_name}</span>
                        </>
                      )}
                    </p>
                  </Link>
                </li>
              )
            })}
          </ul>
          {total > items.length && (
            <p className="mt-2 px-1 text-[11px] text-slate-400">
              The latest {count(items.length)} of {count(total)}.
            </p>
          )}
        </>
      )}
    </div>
  )
}
