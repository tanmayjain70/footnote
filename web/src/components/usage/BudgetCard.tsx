import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ApiError, api } from '../../lib/api'
import { cost, percent } from '../../lib/format'
import type { UsageSummary } from '../../lib/types'
import { Button, Card, ErrorNote, Field, Input, cx } from '../ui'

/** The API's ceiling; above it the request is refused with the same message. */
const MAX_BUDGET_USD = 100_000

type Checked = { ok: true; value: string } | { ok: false; error: string }

/**
 * The API stores the budget in cents, so a third decimal place would be
 * rounded without a word. Better to refuse it here and say why.
 */
function check(text: string): Checked {
  const trimmed = text.trim()
  if (!trimmed) return { ok: false, error: 'Enter an amount in US dollars.' }
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) {
    return {
      ok: false,
      error:
        'Use a plain amount with at most two decimal places, like 5 or 12.50. It cannot be negative.',
    }
  }
  const value = Number(trimmed)
  if (value > MAX_BUDGET_USD) {
    return { ok: false, error: `The daily budget cannot be more than ${cost(MAX_BUDGET_USD)}.` }
  }
  return { ok: true, value: value.toFixed(2) }
}

/*
 * The one number the client owns. It is edited in place and the API
 * answers with the whole summary again, so the tiles and the chart line
 * move with it; the next question checks the new figure.
 */
export function BudgetCard({
  summary,
  onSaved,
}: {
  summary: UsageSummary
  onSaved: (next: UsageSummary) => void
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)

  const save = useMutation({
    // Sent as a string: a JSON number would go through a float on the way
    // to the API's Decimal, and 12.50 is worth keeping as 12.50.
    mutationFn: (daily_budget_usd: string) =>
      api.patch<UsageSummary>('/usage/budget', { daily_budget_usd }),
    onSuccess: (next) => {
      setEditing(false)
      setError(null)
      onSaved(next)
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'The budget could not be saved.'),
  })

  function open() {
    setText(Number(summary.daily_budget_usd).toFixed(2))
    setError(null)
    setEditing(true)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    const checked = check(text)
    if (!checked.ok) {
      setError(checked.error)
      return
    }
    save.mutate(checked.value)
  }

  const budget = Number(summary.daily_budget_usd)
  const spent = Number(summary.spent_today_usd)
  const used = budget > 0 ? Math.min(spent / budget, 1) : spent > 0 ? 1 : 0
  const exhausted = Number(summary.remaining_usd) <= 0

  return (
    <Card
      title="Daily budget"
      subtitle="Once the day's spend reaches it, questions are refused until midnight UTC."
      actions={
        !editing && (
          <Button size="sm" onClick={open}>
            Edit budget
          </Button>
        )
      }
    >
      {editing ? (
        // noValidate: the browser would otherwise refuse 12.25 for not being
        // a multiple of the step. The step is for the spinner; the check
        // above is the rule.
        <form onSubmit={submit} noValidate className="space-y-3">
          <Field
            label="Daily budget (US dollars)"
            hint="Whole cents. The model provider bills in dollars."
          >
            <Input
              type="number"
              min={0}
              max={MAX_BUDGET_USD}
              step={0.5}
              inputMode="decimal"
              value={text}
              onChange={(e) => setText(e.target.value)}
              autoFocus
              required
            />
          </Field>
          {error && <ErrorNote>{error}</ErrorNote>}
          <div className="flex gap-2">
            <Button type="submit" variant="primary" size="sm" disabled={save.isPending}>
              {save.isPending ? 'Saving…' : 'Save budget'}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={save.isPending}
              onClick={() => {
                setEditing(false)
                setError(null)
              }}
            >
              Cancel
            </Button>
          </div>
        </form>
      ) : (
        <div>
          <p className="tabular text-3xl font-semibold text-slate-900">
            {cost(summary.daily_budget_usd)}
          </p>
          <p className="mt-0.5 text-xs text-slate-500">per day</p>
          <div
            className="mt-4 h-1.5 w-full overflow-hidden rounded-full bg-slate-100"
            role="img"
            aria-label={`${percent(used)} of the daily budget used`}
          >
            <span
              className={cx(
                'block h-full',
                used >= 1 ? 'bg-bad-600' : used >= 0.8 ? 'bg-note-600' : 'bg-brand-500',
              )}
              style={{ width: `${used * 100}%` }}
            />
          </div>
          <dl className="tabular mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-slate-500">Spent today</dt>
            <dd className="text-right text-slate-800">{cost(summary.spent_today_usd)}</dd>
            <dt className="text-slate-500">Remaining</dt>
            <dd className={cx('text-right', exhausted ? 'font-medium text-bad-700' : 'text-slate-800')}>
              {cost(summary.remaining_usd)}
            </dd>
          </dl>
        </div>
      )}
    </Card>
  )
}
