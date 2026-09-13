import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import { cost, count, percent } from '../lib/format'
import type { UsageSummary } from '../lib/types'
import { Card, ErrorNote, Spinner, Stat } from '../components/ui'
import { BudgetCard } from '../components/usage/BudgetCard'
import { ByKindTable, ByUserTable } from '../components/usage/Breakdowns'
import { SpendChart } from '../components/usage/SpendChart'

const USAGE_KEY = ['usage', 'summary'] as const

/*
 * What the model has cost and what it may cost. The figures are in US
 * dollars because that is the provider's currency; dressing them up as
 * pounds would be a conversion nobody asked for. Spend moves while the
 * page is open -- every question adds to it -- so it is re-read regularly.
 */
export function UsagePage() {
  const queryClient = useQueryClient()

  const summary = useQuery({
    queryKey: USAGE_KEY,
    queryFn: () => api.get<UsageSummary>('/usage/summary'),
    refetchInterval: 30_000,
  })

  const data = summary.data
  const budget = Number(data?.daily_budget_usd ?? 0)
  const remaining = Number(data?.remaining_usd ?? 0)
  const exhausted = Boolean(data) && remaining <= 0
  const remainingTone = !data
    ? 'slate'
    : exhausted
      ? 'bad'
      : budget > 0 && remaining / budget < 0.2
        ? 'note'
        : 'good'
  const callsToday = data?.by_day[data.by_day.length - 1]?.calls ?? 0

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Usage</h1>
        <p className="mt-0.5 max-w-3xl text-sm text-slate-500">
          What the model provider has been paid for answers, extractions and evaluation runs, and
          the daily limit that stops it. Figures are in US dollars, as billed.
        </p>
      </div>

      {summary.isLoading ? (
        <Spinner label="Loading usage" />
      ) : summary.error ? (
        <ErrorNote>
          {summary.error instanceof ApiError
            ? summary.error.message
            : 'The usage summary could not be loaded.'}
        </ErrorNote>
      ) : data ? (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Stat
              label="Spent today"
              value={cost(data.spent_today_usd)}
              hint={`${count(callsToday)} model call${callsToday === 1 ? '' : 's'} since midnight UTC`}
            />
            <Stat
              label="Daily budget"
              value={cost(data.daily_budget_usd)}
              hint="Resets at midnight UTC"
            />
            <Stat
              label="Remaining today"
              value={cost(data.remaining_usd)}
              tone={remainingTone}
              hint={
                exhausted
                  ? 'Budget reached: questions are refused until it resets'
                  : budget > 0
                    ? `${percent(remaining / budget)} of the budget left`
                    : undefined
              }
            />
          </div>

          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
            <Card
              title="Spend by day"
              subtitle={`The last ${count(data.days)} days. Hover a bar for the day's calls.`}
            >
              <SpendChart days={data.by_day} budget={data.daily_budget_usd} />
            </Card>
            <BudgetCard
              summary={data}
              onSaved={(next) => {
                queryClient.setQueryData(USAGE_KEY, next)
                // The header reads the budget from /health; keep it in step.
                queryClient.invalidateQueries({ queryKey: ['health'] })
              }}
            />
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <ByKindTable rows={data.by_kind} days={data.days} />
            <ByUserTable rows={data.by_user} days={data.days} />
          </div>
        </>
      ) : null}
    </div>
  )
}
