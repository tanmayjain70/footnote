import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { count, dateTime } from '../lib/format'
import type { QuestionOut } from '../lib/types'
import { Button, Card, Empty, Kbd, Select, Spinner, Textarea } from '../components/ui'
import { AnswerView } from '../components/ask/AnswerView'
import { FeedbackBar } from '../components/ask/FeedbackBar'
import { QuestionHistory } from '../components/ask/QuestionHistory'
import { SourcesPanel } from '../components/ask/SourcesPanel'
import { IDLE, fromStored, statusOf, useAsk } from '../components/ask/useAsk'

/*
 * Three columns: what was asked before, the question and its answer, and the
 * passages the model was shown. The answer and the passages are one screen on
 * purpose -- a citation is only worth anything if checking it costs a glance,
 * not a navigation.
 *
 * `/` streams a new answer; `/questions/:id` shows a stored one. Both render
 * through the same state shape, so a stored answer is pixel-for-pixel what the
 * asker saw when it finished.
 */

const MIN_CHARS = 3
const MAX_CHARS = 2000
const TEXTAREA_ID = 'ask-question'

const EXAMPLES = [
  'When does the lease of Unit 4, Meridian House expire?',
  'What notice must the tenant give to exercise the break at Whitworth Court?',
  'Who is the tenant at 12 Deansgate, and what is the annual rent?',
]

export function AskPage() {
  const { me } = useAuth()
  const { id: routeId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const canAsk = Boolean(me?.permissions.can_ask)
  const portfolios = me?.portfolios ?? []

  const { state, ask, stop, replace, resetIfDone } = useAsk()
  const [text, setText] = useState('')
  const [scope, setScope] = useState('')
  const [active, setActive] = useState<number | null>(null)

  // The route can change while an answer is in flight; the handler that
  // started it must not navigate away from wherever the person went.
  const routeRef = useRef(routeId)
  routeRef.current = routeId

  // The live answer owns the screen on `/`, and on `/questions/:id` when it
  // is that question. Anything else is loaded from the API.
  const showLive = !routeId || state.questionId === routeId
  const stored = useQuery({
    queryKey: ['question', routeId],
    queryFn: () => api.get<QuestionOut>(`/questions/${routeId}`),
    enabled: Boolean(routeId) && !showLive,
  })
  const view = useMemo(
    () => (showLive ? state : stored.data ? fromStored(stored.data) : IDLE),
    [showLive, state, stored.data],
  )
  const status = statusOf(view)

  // Opening a stored question while an answer is arriving abandons the stream:
  // the person has moved on, and the row stays whatever the server made of it.
  // Only a change of route counts. Asking again from `/questions/:id` starts
  // the stream before the router has applied the move to `/` (navigation is a
  // transition, the stream state is not), and for that one render the old id
  // is still the route; reacting to it would abort the question just asked.
  const lastRoute = useRef(routeId)
  useEffect(() => {
    const changed = lastRoute.current !== routeId
    lastRoute.current = routeId
    if (changed && routeId && state.phase === 'streaming' && state.questionId !== routeId) stop()
  }, [routeId, state.phase, state.questionId, stop])

  // Coming back to a bare `/` (the nav link, "New question") clears the last
  // finished answer. A stream still running is left alone.
  const previousRoute = useRef(routeId)
  useEffect(() => {
    if (previousRoute.current && !routeId) resetIfDone()
    previousRoute.current = routeId
  }, [routeId, resetIfDone])

  // A selected citation belongs to one answer.
  useEffect(() => setActive(null), [view.questionId, view.phase])

  async function submit() {
    const question = text.trim()
    if (!canAsk || question.length < MIN_CHARS) return
    if (routeId) navigate('/', { replace: true })
    setText('')
    const result = await ask(question, scope || null)
    if (!result) return
    queryClient.setQueryData(['question', result.id], result)
    queryClient.invalidateQueries({ queryKey: ['questions'] })
    queryClient.invalidateQueries({ queryKey: ['health'] })
    // The URL becomes the answer's, so a refresh or a shared link shows it
    // again. Only if the person is still here, though.
    if (!routeRef.current) navigate(`/questions/${result.id}`, { replace: true })
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    void submit()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault()
      void submit()
    }
  }

  function askAgain() {
    setText(view.question)
    const textarea = document.getElementById(TEXTAREA_ID)
    if (textarea instanceof HTMLTextAreaElement) {
      textarea.focus()
      textarea.setSelectionRange(textarea.value.length, textarea.value.length)
    }
  }

  function onFeedback(updated: QuestionOut) {
    queryClient.setQueryData(['question', updated.id], updated)
    if (showLive) replace(updated)
  }

  const streaming = status === 'streaming'
  const ready = canAsk && text.trim().length >= MIN_CHARS
  const scopeName = view.portfolioId
    ? (portfolios.find((portfolio) => portfolio.id === view.portfolioId)?.name ?? 'one portfolio')
    : 'every portfolio you can see'
  const result = view.result
  const feedbackAllowed =
    canAsk && result !== null && (status === 'answered' || status === 'unanswered')

  const storedError =
    !showLive && stored.error
      ? stored.error instanceof ApiError && stored.error.status === 404
        ? 'That question is not one you can see. It may be someone else’s, or it may have been removed.'
        : stored.error instanceof ApiError
          ? stored.error.message
          : 'Could not load that question.'
      : null

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Ask</h1>
        <p className="mt-0.5 max-w-3xl text-sm text-slate-500">
          Ask about a lease in plain English. The answer cites the page it came from, every
          citation is checked against the passage the model was shown, and when the documents do
          not say, it says so.
        </p>
      </div>

      {/* Stacked on a phone the form comes first; the history is a sidebar only when there is room for one. */}
      <div className="grid gap-5 lg:grid-cols-[280px_minmax(0,1fr)] xl:grid-cols-[280px_minmax(0,1fr)_320px]">
        <aside className="order-2 min-w-0 lg:order-none">
          <QuestionHistory
            currentId={view.questionId}
            enabled={canAsk}
            viewerId={me?.user.id}
          />
        </aside>

        <div className="order-1 min-w-0 space-y-5 lg:order-none">
          <Card>
            <form onSubmit={onSubmit} className="space-y-3">
              <Textarea
                id={TEXTAREA_ID}
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={onKeyDown}
                rows={3}
                maxLength={MAX_CHARS}
                disabled={!canAsk}
                placeholder={
                  canAsk
                    ? 'e.g. What notice does the tenant have to give to use the break at Whitworth Court?'
                    : 'Your role cannot ask questions.'
                }
                aria-label="Your question"
              />
              <div className="flex flex-wrap items-center gap-3">
                <Select
                  value={scope}
                  onChange={(e) => setScope(e.target.value)}
                  disabled={!canAsk}
                  aria-label="Portfolio scope"
                >
                  <option value="">Every portfolio I can see</option>
                  {portfolios.map((portfolio) => (
                    <option key={portfolio.id} value={portfolio.id}>
                      {portfolio.name}
                      {portfolio.confidential ? ' (confidential)' : ''} ·{' '}
                      {count(portfolio.document_count)} document
                      {portfolio.document_count === 1 ? '' : 's'}
                    </option>
                  ))}
                </Select>
                <span className="hidden text-xs text-slate-400 sm:inline">
                  <Kbd>Ctrl</Kbd> + <Kbd>Enter</Kbd> to ask
                </span>
                <div className="ml-auto flex items-center gap-2">
                  {streaming && (
                    <Button type="button" size="md" variant="ghost" onClick={stop}>
                      Stop
                    </Button>
                  )}
                  <Button type="submit" variant="primary" disabled={!ready}>
                    {streaming ? 'Ask instead' : 'Ask'}
                  </Button>
                </div>
              </div>
              {!canAsk && (
                <p className="text-xs text-slate-500">
                  Finance reads the register and the documents; questions spend the model budget
                  and are for managers and administrators.
                </p>
              )}
            </form>
          </Card>

          {!showLive && stored.isLoading ? (
            <Spinner label="Loading the answer" />
          ) : storedError ? (
            <Empty title="Question not available">{storedError}</Empty>
          ) : status === 'idle' ? (
            <Empty title="Ask something about a lease">
              {canAsk ? (
                <ul className="mt-2 space-y-1">
                  {EXAMPLES.map((example) => (
                    <li key={example}>
                      <button
                        type="button"
                        className="text-brand-700 hover:underline"
                        onClick={() => setText(example)}
                      >
                        {example}
                      </button>
                    </li>
                  ))}
                  <li className="pt-1 text-slate-400">
                    Questions of the form “which leases have a break in 2027?” are a table, not a
                    chat: use the Register.
                  </li>
                </ul>
              ) : (
                'Pick a question on the left to read its answer.'
              )}
            </Empty>
          ) : (
            <Card
              title={<span className="text-base leading-snug font-medium">{view.question}</span>}
              subtitle={
                result ? (
                  <>
                    {result.user_name ?? 'Someone'} · {dateTime(result.created_at)} · {scopeName}
                  </>
                ) : (
                  <>Asking across {scopeName}</>
                )
              }
              actions={
                canAsk && !streaming ? (
                  <Button size="sm" variant="ghost" onClick={askAgain}>
                    Edit and ask again
                  </Button>
                ) : undefined
              }
            >
              <AnswerView state={view} status={status} active={active} onCite={setActive} />
              {feedbackAllowed && result && (
                <div className="mt-5 border-t border-slate-100 pt-4">
                  <FeedbackBar key={result.id} question={result} onSaved={onFeedback} />
                </div>
              )}
            </Card>
          )}
        </div>

        <aside className="order-3 min-w-0 lg:order-none lg:col-start-2 xl:col-start-auto">
          <SourcesPanel
            sources={view.sources}
            citations={view.citations}
            status={status}
            active={active}
            onSelect={setActive}
          />
        </aside>
      </div>
    </div>
  )
}
