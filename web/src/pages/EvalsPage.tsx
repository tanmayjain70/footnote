import { Fragment, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api, query } from '../lib/api'
import { useAuth } from '../lib/auth'
import { cost, count } from '../lib/format'
import type { EvalMode, EvalQuestion, EvalRun, EvalRunRequest, Page } from '../lib/types'
import {
  Button,
  Card,
  Empty,
  ErrorNote,
  Input,
  Notice,
  Pager,
  Spinner,
  Stat,
} from '../components/ui'
import { AddQuestionForm } from '../components/evals/AddQuestionForm'
import { GoldenSet, QUESTIONS_KEY, summarise } from '../components/evals/GoldenSet'
import { METRICS } from '../components/evals/metrics'
import { RunsTable } from '../components/evals/RunsTable'

const RUNS_LIMIT = 20
/** The largest page the API serves. The set is a few hundred questions, so one or two requests fetch it whole. */
const QUESTIONS_PAGE = 500
/** The API's ceiling on a run's `limit`. */
const MAX_RUN_LIMIT = 1000

/*
 * The whole set in one array rather than a page at a time: the tiles count
 * every question, the table filters and pages on the client, and the active
 * toggle patches one entry in place. Three server-side queries over the
 * same list could disagree with each other; one array cannot.
 */
async function fetchAllQuestions(): Promise<EvalQuestion[]> {
  const items: EvalQuestion[] = []
  let offset = 0
  for (;;) {
    const page = await api.get<Page<EvalQuestion>>(
      `/evals/questions${query({ limit: QUESTIONS_PAGE, offset })}`,
    )
    items.push(...page.items)
    offset += page.items.length
    if (page.items.length === 0 || offset >= page.total) return items
  }
}

function isRunning(run: EvalRun): boolean {
  return run.status === 'running'
}

type Limit = { value: number | null; error: null } | { value: null; error: string }

/** Blank means the whole set; anything else must be a whole number the API will accept. */
function parseLimit(text: string): Limit {
  const trimmed = text.trim()
  if (!trimmed) return { value: null, error: null }
  if (!/^\d+$/.test(trimmed) || Number(trimmed) < 1) {
    return { value: null, error: 'The limit must be a whole number of questions, at least 1.' }
  }
  return { value: Math.min(Number(trimmed), MAX_RUN_LIMIT), error: null }
}

/*
 * The golden set and the runs scored against it. A retrieval run is free
 * and finishes before the request returns; an end-to-end run asks the
 * model every question, costs real money, and is followed here while the
 * worker gets through it.
 */
export function EvalsPage() {
  const { me } = useAuth()
  const queryClient = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [limitText, setLimitText] = useState('')
  const [runError, setRunError] = useState<string | null>(null)

  const questions = useQuery({
    queryKey: QUESTIONS_KEY,
    queryFn: fetchAllQuestions,
  })

  const runs = useQuery({
    queryKey: ['evals', 'runs', { offset }],
    queryFn: () =>
      api.get<Page<EvalRun>>(`/evals/runs${query({ limit: RUNS_LIMIT, offset })}`),
    // An end-to-end run answers its questions in the worker. While one is
    // going, the list follows it so the totals fill in without a reload.
    refetchInterval: (current) => (current.state.data?.items.some(isRunning) ? 3000 : false),
    placeholderData: keepPreviousData,
  })

  const start = useMutation({
    mutationFn: (body: EvalRunRequest) => api.post<EvalRun>('/evals/runs', body),
    onSuccess: (run) => {
      setRunError(null)
      setOffset(0)
      setExpandedId(run.id)
      queryClient.invalidateQueries({ queryKey: ['evals', 'runs'] })
      // The header shows today's spend against the budget; an end-to-end
      // run moves it.
      queryClient.invalidateQueries({ queryKey: ['health'] })
    },
    onError: (err) =>
      setRunError(err instanceof ApiError ? err.message : 'The run could not be started.'),
  })

  const summary = summarise(questions.data ?? [])
  const limit = parseLimit(limitText)
  const items = runs.data?.items ?? []
  const anyRunning = items.some(isRunning)
  const toRun = limit.value ? Math.min(limit.value, summary.active) : summary.active

  // A rough price for the next end-to-end run, from the last one that
  // finished. Nothing else on the page knows what a question costs.
  const lastEndToEnd = items.find(
    (run) =>
      run.mode === 'end_to_end' &&
      run.status === 'done' &&
      typeof run.totals.questions === 'number' &&
      run.totals.questions > 0,
  )
  const perQuestion =
    lastEndToEnd && typeof lastEndToEnd.totals.questions === 'number'
      ? Number(lastEndToEnd.totals.cost_usd) / lastEndToEnd.totals.questions
      : null
  const estimate =
    perQuestion !== null && Number.isFinite(perQuestion) ? perQuestion * toRun : null

  function startRun(mode: EvalMode) {
    if (limit.error) {
      setRunError(limit.error)
      return
    }
    if (questions.data && summary.active === 0) {
      setRunError('There are no active questions to score. Add one, or switch one back on below.')
      return
    }
    start.mutate({ mode, limit: limit.value })
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Evaluation</h1>
        <p className="mt-0.5 max-w-3xl text-sm text-slate-500">
          A set of questions with known answers, and the runs scored against it. A retrieval run
          checks whether the right passage is found; an end-to-end run also asks the model and
          checks what it cited, or that it refused when it should.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat
          label="Active questions"
          value={questions.data ? count(summary.active) : '—'}
          hint={
            questions.data && summary.inactive > 0
              ? `${count(summary.inactive)} switched off`
              : 'Every run scores these'
          }
        />
        <Stat
          label="Answerable"
          value={questions.data ? count(summary.answerable) : '—'}
          hint="Each names the document and pages that answer it"
        />
        <Stat
          label="Unanswerable"
          value={questions.data ? count(summary.unanswerable) : '—'}
          hint="The right answer is a refusal"
        />
      </div>

      <AddQuestionForm
        portfolios={me?.portfolios ?? []}
        onCreated={(created) => {
          queryClient.setQueryData<EvalQuestion[]>(QUESTIONS_KEY, (current) =>
            current ? [created, ...current] : [created],
          )
          queryClient.invalidateQueries({ queryKey: QUESTIONS_KEY })
        }}
      />

      <Card
        title="Runs"
        subtitle="Newest first. Open a row for the result on every question."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <span className="whitespace-nowrap">First</span>
              <Input
                type="number"
                min={1}
                max={MAX_RUN_LIMIT}
                inputMode="numeric"
                value={limitText}
                onChange={(e) => setLimitText(e.target.value)}
                placeholder="all"
                aria-label="Score only the first N active questions"
                className="w-20"
              />
              <span className="whitespace-nowrap">questions</span>
            </label>
            <Button
              size="sm"
              variant="primary"
              onClick={() => startRun('retrieval')}
              disabled={start.isPending}
              title="Searches for every question and checks what came back. Costs nothing."
            >
              Run retrieval evaluation
            </Button>
            <Button
              size="sm"
              onClick={() => startRun('end_to_end')}
              disabled={start.isPending}
              title="Asks the model every question. Spends money."
            >
              Run end-to-end evaluation
            </Button>
          </div>
        }
      >
        <div className="space-y-3">
          <Notice tone="note" title="An end-to-end run spends money">
            It asks the model every active question
            {questions.data ? ` (${count(toRun)} of them)` : ''} and the spend counts against the
            daily budget like any other question.{' '}
            {estimate !== null && perQuestion !== null
              ? `The last one cost ${cost(perQuestion)} a question, so this should come to about ${cost(estimate)}.`
              : 'A retrieval run only searches, and costs nothing.'}{' '}
            Set a limit to try a handful first.
          </Notice>
          {anyRunning && (
            <Notice tone="slate">
              A run is still going. The table checks every few seconds until it finishes.
            </Notice>
          )}
          {runError && <ErrorNote>{runError}</ErrorNote>}
          {start.isPending && <Spinner label="Starting the run" />}

          {runs.isLoading ? (
            <Spinner label="Loading runs" />
          ) : runs.error ? (
            <ErrorNote>
              {runs.error instanceof ApiError ? runs.error.message : 'The runs could not be loaded.'}
            </ErrorNote>
          ) : !items.length ? (
            <Empty title="No runs yet">
              Run a retrieval evaluation to see how often the right passage is found. It costs
              nothing and finishes in seconds.
            </Empty>
          ) : (
            <>
              <RunsTable
                runs={items}
                expandedId={expandedId}
                onToggle={(id) => setExpandedId((current) => (current === id ? null : id))}
              />
              <Pager
                total={runs.data?.total ?? 0}
                limit={RUNS_LIMIT}
                offset={offset}
                onChange={(next) => {
                  setOffset(next)
                  setExpandedId(null)
                }}
              />
            </>
          )}

          <dl className="grid gap-x-4 gap-y-1 border-t border-slate-100 pt-3 text-xs sm:grid-cols-[auto_1fr]">
            {METRICS.map((metric) => (
              <Fragment key={metric.key}>
                <dt className="font-medium whitespace-nowrap text-slate-700">{metric.label}</dt>
                <dd className="text-slate-500">{metric.explain}</dd>
              </Fragment>
            ))}
          </dl>
        </div>
      </Card>

      <GoldenSet
        questions={questions.data}
        portfolios={me?.portfolios ?? []}
        loading={questions.isLoading}
        error={questions.error}
      />
    </div>
  )
}
