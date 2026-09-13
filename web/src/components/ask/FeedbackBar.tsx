import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ApiError, api } from '../../lib/api'
import type { AnswerOut, FeedbackReason, FeedbackRequest, QuestionOut } from '../../lib/types'
import { Button, ErrorNote, Field, Input, Select } from '../ui'

/*
 * Thumbs up or down on a finished answer. A "down" asks for a reason, because
 * "wrong" and "it should have said it did not know" are opposite failures and
 * the eval set is built from these. Saving is an upsert, so changing your mind
 * is one click, not a second row.
 */

const REASONS: { value: FeedbackReason; label: string }[] = [
  { value: 'wrong', label: 'The answer is wrong' },
  { value: 'missing_citation', label: 'A claim has no citation' },
  { value: 'incomplete', label: 'The answer is incomplete' },
  { value: 'should_have_refused', label: 'It should have said it did not know' },
  { value: 'other', label: 'Something else' },
]

function reasonLabel(reason: FeedbackReason | null): string | null {
  return REASONS.find((option) => option.value === reason)?.label ?? null
}

export function FeedbackBar({
  question,
  onSaved,
}: {
  question: AnswerOut
  /** The API returns the whole question with the feedback applied; callers keep it. */
  onSaved: (updated: QuestionOut) => void
}) {
  const current = question.feedback
  const [editing, setEditing] = useState(false)
  const [reasonOpen, setReasonOpen] = useState(false)
  const [reason, setReason] = useState<FeedbackReason>(current?.reason ?? 'wrong')
  const [note, setNote] = useState(current?.note ?? '')

  const save = useMutation({
    mutationFn: (body: FeedbackRequest) =>
      api.post<QuestionOut>(`/questions/${question.id}/feedback`, body),
    onSuccess: (updated) => {
      setEditing(false)
      setReasonOpen(false)
      onSaved(updated)
    },
  })

  function sendDown(event: FormEvent) {
    event.preventDefault()
    save.mutate({ verdict: 'down', reason, note: note.trim() || null })
  }

  const error =
    save.error instanceof ApiError
      ? save.error.message
      : save.error
        ? 'Could not save your feedback.'
        : null

  if (current && !editing) {
    const label = reasonLabel(current.reason)
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
        <span>
          You marked this {current.verdict === 'up' ? 'helpful' : 'not helpful'}
          {label && <> — {label.toLowerCase()}</>}
          {current.note && <> — “{current.note}”</>}.
        </span>
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
          Change
        </Button>
      </div>
    )
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span>Was this answer right?</span>
        <Button
          size="sm"
          variant={current?.verdict === 'up' ? 'primary' : 'secondary'}
          disabled={save.isPending}
          onClick={() => save.mutate({ verdict: 'up', reason: null, note: null })}
        >
          Helpful
        </Button>
        <Button
          size="sm"
          variant={current?.verdict === 'down' ? 'primary' : 'secondary'}
          disabled={save.isPending}
          aria-expanded={reasonOpen}
          onClick={() => setReasonOpen((open) => !open)}
        >
          Not helpful
        </Button>
        {editing && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setEditing(false)
              setReasonOpen(false)
            }}
          >
            Cancel
          </Button>
        )}
      </div>

      {reasonOpen && (
        <form onSubmit={sendDown} className="mt-3 flex flex-wrap items-end gap-3">
          <Field label="What went wrong?">
            <Select value={reason} onChange={(e) => setReason(e.target.value as FeedbackReason)}>
              {REASONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
          <div className="w-72 max-w-full">
            <Field label="Note (optional)">
              <Input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                maxLength={500}
                placeholder="e.g. the notice period is six months, see clause 12"
              />
            </Field>
          </div>
          <Button type="submit" size="sm" variant="primary" disabled={save.isPending}>
            {save.isPending ? 'Saving…' : 'Send'}
          </Button>
        </form>
      )}

      {error && (
        <div className="mt-2">
          <ErrorNote>{error}</ErrorNote>
        </div>
      )}
    </div>
  )
}
